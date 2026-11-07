"""User-triggered import of a displayed BOSS job detail via Windows accessibility.

Reads the selected document only. No cookies, DevTools, private endpoints, page
scripts, clicks, scrolling, messaging, or application submission are used.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

from PIL import Image, ImageGrab

from job_agent.settings import PRIVATE, atomic_write
from job_agent.browser_lock import BrowserSessionBusy, browser_session_lock


class CaptureError(ValueError):
    pass


class CaptureStopped(CaptureError):
    """A changed page or access restriction stops the whole collection batch."""


def detail_url(value: str) -> str:
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or p.hostname not in {'www.zhipin.com', 'zhipin.com'} or p.username or p.password or p.port not in {None, 443}:
            raise ValueError()
        if not re.fullmatch(r'/job_detail/[A-Za-z0-9_-]+\.html', p.path):
            raise ValueError()
        # Tracking/security query strings are never stored or sent to models.
        return urlunsplit(('https', 'www.zhipin.com', p.path, '', ''))
    except (ValueError, TypeError):
        raise CaptureError('请在 Edge 或 Chrome 的当前标签页打开一个 BOSS 岗位详情页。') from None


def rect(node):
    value = node.rectangle()
    return (value.left, value.top, value.right, value.bottom)


def union_rect(rectangles):
    if not rectangles or any(r[2] <= r[0] or r[3] <= r[1] for r in rectangles):
        raise CaptureError('岗位关键区域没有完整显示，请将详情页滚动至顶部。')
    return (min(r[0] for r in rectangles), min(r[1] for r in rectangles),
            max(r[2] for r in rectangles), max(r[3] for r in rectangles))


def compose_evidence(image: Image.Image, window_rect, document_rect, regions):
    """Preserve pixels of the two proven job regions; never retain a full window."""
    if image.size != (window_rect[2] - window_rect[0], window_rect[3] - window_rect[1]):
        raise CaptureError('浏览器截图坐标不一致，请重新打开详情页后再试。')
    crops = []
    for region in regions:
        if any(region[i + 2] <= region[i] for i in range(2)):
            raise CaptureError('岗位截图区域无效。')
        if not (document_rect[0] <= region[0] < region[2] <= document_rect[2] and
                document_rect[1] <= region[1] < region[3] <= document_rect[3] and
                window_rect[0] <= region[0] < region[2] <= window_rect[2] and
                window_rect[1] <= region[1] < region[3] <= window_rect[3]):
            raise CaptureError('关键区域未完整显示，请将岗位详情页滚动至顶部。')
        box = (region[0] - window_rect[0], region[1] - window_rect[1],
               region[2] - window_rect[0], region[3] - window_rect[1])
        crop = image.crop(box).convert('RGB')
        if sum(hi - lo for lo, hi in crop.getextrema()) < 30:
            raise CaptureError('浏览器返回空白截图，请保持岗位页面可见后再试。')
        crops.append(crop)
    if len(crops) not in {2, 3}:
        raise CaptureError('缺少岗位或公司信息截图。')
    result = Image.new('RGB', (max(c.width for c in crops) + 32, sum(c.height for c in crops) + 16 * (len(crops) + 1)), '#ffffff')
    top = 16
    for crop in crops:
        result.paste(crop, (16, top))
        top += crop.height + 16
    return result


def text_between(document, first, last):
    span = document.iface_text.RangeFromChild(first.element_info.element)
    end = document.iface_text.RangeFromChild(last.element_info.element)
    span.MoveEndpointByRange(1, end, 0)
    value = span.GetText(100001)
    if len(value) > 100000:
        raise CaptureError('岗位文字过长，已停止读取。')
    return value.replace('\ufffc', '').strip()


def validate_job_text(header: str, company: str, description: str):
    text = '\n\n'.join([header, company, description])
    if len(description) < 40 or '职位描述' not in description or '公司基本信息' not in company:
        raise CaptureError('没有读取到完整岗位与公司区域，请检查页面是否加载完成。')
    if re.search(r'完成.{0,8}验证|安全验证|访问过于频繁|操作过于频繁|账号异常|滑动.{0,8}验证', text):
        raise CaptureStopped('页面出现验证或访问限制，已停止；请在浏览器中自行处理。')
    if not re.search(r'\d+\s*(?:[-～~至—]\s*\d+)?\s*人', company):
        raise CaptureError('公司区域没有明确人数，保留人工导入以便记录未知状态。')
    return text


def _browser_process(pid: int) -> bool:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        return Path(buffer.value).name.casefold() in {'msedge.exe', 'chrome.exe'}
    finally:
        kernel.CloseHandle(handle)


def visible_region_capture(window, window_bounds, regions):
    """Capture only the selected regions when Chromium window capture is blank."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    window.set_focus()
    def verify():
        if user.GetForegroundWindow() != window.handle:
            raise CaptureError('浏览器未在前台，已停止截图；请保持岗位详情可见。')
        for left, top, right, bottom in regions:
            for x in list(range(left + 1, right, 64)) + [right - 1]:
                for y in list(range(top + 1, bottom, 64)) + [bottom - 1]:
                    hit = user.WindowFromPoint(wintypes.POINT(x, y))
                    if user.GetAncestor(hit, 2) != window.handle:
                        raise CaptureError('岗位区域被其他窗口遮挡，已停止截图。')
    verify()
    result = Image.new('RGB', (window_bounds[2] - window_bounds[0], window_bounds[3] - window_bounds[1]))
    for region in regions:
        crop = ImageGrab.grab(bbox=region, all_screens=True)
        result.paste(crop, (region[0] - window_bounds[0], region[1] - window_bounds[1]))
    verify()
    return result


def capture_current(directory: Path, *, expected_handle: int | None = None, expected_url: str | None = None) -> dict:
    if os.name != 'nt':
        raise CaptureError('浏览器页面导入目前仅支持 Windows。')
    started_at = datetime.now(timezone.utc).isoformat()
    # Loaded only by this command, never on importing the workbench or in offline tests.
    from pywinauto import Desktop
    candidates = []
    for window in Desktop(backend='win32').windows(visible_only=True):
        if expected_handle is not None and window.handle != expected_handle:
            continue
        if window.class_name() != 'Chrome_WidgetWin_1' or window.is_minimized() or not _browser_process(window.process_id()):
            continue
        current = Desktop(backend='uia').window(handle=window.handle)
        for document in current.descendants(control_type='Document'):
            try:
                url = detail_url(str(document.iface_value.CurrentValue))
                bounds = rect(document)
                if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                    candidates.append((window, document, url))
            except Exception:
                continue
    if not candidates:
        raise CaptureError('没有可读取的岗位详情页。请在 Edge / Chrome 当前标签页打开 BOSS 岗位详情并滚动至顶部；搜索列表、登录和验证页不导入。')
    if len(candidates) != 1:
        raise CaptureError('有多个浏览器窗口显示岗位详情；请只保留一个详情窗口可见，其余最小化。')
    window, document, url = candidates[0]
    if expected_url is not None and url != detail_url(expected_url):
        raise CaptureStopped('浏览器已离开本次目标岗位，已停止自动采集。')
    nodes = document.descendants()
    primary = [n for n in nodes if n.class_name() == 'info-primary']
    if len(primary) != 1:
        raise CaptureError('岗位详情布局已变化，无法安全定位；请使用文字和截图手动导入。')
    heading_nodes = [n for n in primary[0].descendants() if (n.element_info.control_type == 'Text' or n.class_name() == 'text-desc text-city') and n.window_text().strip()]
    controls = [n for n in primary[0].descendants() if n.class_name() == 'btn-container']
    if controls:
        heading_nodes = [n for n in heading_nodes if rect(n)[3] <= rect(controls[0])[1]]
    if not heading_nodes:
        raise CaptureError('没有识别到岗位标题区域。')
    header = '\n'.join(n.window_text().strip() for n in heading_nodes)
    anchors = [n for n in nodes if n.element_info.control_type == 'Text' and n.window_text() == '公司基本信息']
    ends = [n for n in nodes if n.element_info.control_type == 'Hyperlink' and n.window_text().strip() == '查看全部职位']
    descriptions = [n for n in nodes if n.element_info.control_type == 'Text' and n.window_text() == '职位描述']
    recruiters = [n for n in nodes if n.element_info.control_type == 'Text' and n.class_name() == 'name']
    if not all(len(x) == 1 for x in [anchors, ends, descriptions, recruiters]):
        raise CaptureError('无法唯一定位岗位或公司区域，请使用手动导入。')
    company = text_between(document, anchors[0], ends[0])
    description = text_between(document, descriptions[0], recruiters[0])
    text = validate_job_text(header, company, description)
    left, top, _, _ = rect(anchors[0])
    stop = rect(ends[0])[1]
    company_nodes = [n for n in nodes if n.element_info.control_type in {'Text', 'Hyperlink'}
                     and n.window_text().strip() and rect(n)[0] >= left - 2
                     and rect(n)[1] >= top and rect(n)[3] <= stop]
    doc_bounds = rect(document)
    regions = [union_rect([rect(n) for n in heading_nodes]), union_rect([rect(n) for n in company_nodes])]
    # Include visible description pixels because the large page title may be ellipsized.
    desc_box = (rect(descriptions[0])[0], rect(descriptions[0])[1], rect(recruiters[0])[2],
                min(rect(recruiters[0])[1] - 8, doc_bounds[3] - 8))
    if desc_box[3] > desc_box[1] + 80:
        regions.append(desc_box)
    regions = [(max(doc_bounds[0], a - 3), max(doc_bounds[1], b - 3),
                min(doc_bounds[2], c + 3), min(doc_bounds[3], e + 3)) for a, b, c, e in regions]
    dwm = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(window.handle, 9, ctypes.byref(dwm), ctypes.sizeof(dwm)) != 0:
        raise CaptureError('无法读取浏览器截图坐标。')
    window_bounds = (dwm.left, dwm.top, dwm.right, dwm.bottom)
    snapshot = ImageGrab.grab(window=window.handle)
    if sum(hi - lo for lo, hi in snapshot.convert('RGB').getextrema()) < 30:
        snapshot = visible_region_capture(window, window_bounds, regions)
    evidence = compose_evidence(snapshot, window_bounds, doc_bounds, regions)
    del snapshot
    if detail_url(str(document.iface_value.CurrentValue)) != url or header != '\n'.join(n.window_text().strip() for n in heading_nodes) or company != text_between(document, anchors[0], ends[0]) or description != text_between(document, descriptions[0], recruiters[0]):
        raise CaptureStopped('读取期间页面发生变化，未保存；请保持岗位页面稳定后再试。')
    directory.mkdir(parents=True, exist_ok=True)
    screenshot = directory / 'job-evidence.png'
    evidence.save(screenshot)
    result = {'url': url, 'text': text, 'screenshot': str(screenshot.resolve()),
              'capture_started_at': started_at, 'captured_at': datetime.now(timezone.utc).isoformat(),
              'window_rect': window_bounds, 'document_rect': doc_bounds, 'crop_rects': regions,
              'method': 'windows_uia_current_detail', 'screenshot_scope': 'job_header_company_and_visible_description_pixel_crops',
              'screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest(),
              'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
              'sent': False}
    atomic_write(directory / 'capture.json', json.dumps(result, ensure_ascii=False, indent=2).encode())
    return result


def main():
    parser = argparse.ArgumentParser(description='Import the currently displayed job detail without sending.')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--private', type=Path, default=PRIVATE)
    args = parser.parse_args()
    try:
        with browser_session_lock(args.private):
            result = capture_current(args.output_dir)
        print(json.dumps({'ok': True, 'url': result['url']}, ensure_ascii=False))
        return 0
    except (CaptureError, BrowserSessionBusy) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({'ok': False, 'error': '浏览器读取失败；请保持完整岗位详情可见，或使用手动导入。'}, ensure_ascii=False))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
