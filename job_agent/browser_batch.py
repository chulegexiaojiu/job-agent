"""Bounded collection of visible BOSS results using public browser UI only.

Optional bounded list scrolling/next-page traversal via native UI Automation.
No login handling, cookies, page scripts, chat or submission in this collector.
Each target must have appeared in a visible list snapshot during this bounded scan.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from job_agent.browser_capture import (CaptureError, CaptureStopped, _browser_process,
                                       capture_current, detail_url, rect)
from job_agent.settings import PRIVATE, atomic_write
from job_agent.browser_lock import BrowserSessionBusy, browser_session_lock

class PageLoading(CaptureError):
    """Transient empty accessibility tree during our own navigation only."""


MAX_JOBS = 5
RESTRICTED = re.compile(r'安全验证|请.{0,6}验证|访问过于频繁|操作过于频繁|账号异常|滑动.{0,8}验证|访问受限|扫码登录|请先登录')


def page_url(value: str) -> str:
    """Allow only BOSS search and detail pages; strip tracking and queries."""
    try:
        return detail_url(value)
    except CaptureError:
        pass
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or p.hostname not in {'zhipin.com', 'www.zhipin.com'}
                or p.username or p.password or p.port not in {None, 443}
                or p.path.rstrip('/') != '/web/geek/jobs'):
            raise ValueError()
        return urlunsplit(('https', 'www.zhipin.com', '/web/geek/jobs', '', ''))
    except (ValueError, TypeError):
        raise CaptureStopped('当前页面不是 BOSS 搜索列表或岗位详情，已停止采集。') from None


def contained(box, viewport) -> bool:
    return (viewport[0] <= box[0] < box[2] <= viewport[2]
            and viewport[1] <= box[1] < box[3] <= viewport[3])


def select_candidates(records: list[dict], viewport, known: set[str], limit: int) -> tuple[list[dict], int]:
    if type(limit) is not int or not 1 <= limit <= MAX_JOBS:
        raise CaptureError('每轮采集数量必须为 1～5。')
    selected, seen, duplicates = [], set(), 0
    for record in records:
        if record.get('class') != 'job-name' or not contained(record['rect'], viewport):
            continue
        try:
            url = detail_url(record['url'])
        except CaptureError:
            continue
        if url in seen:
            continue
        seen.add(url)
        if url in known:
            duplicates += 1
        elif len(selected) < limit:
            selected.append({'url': url, 'label': record.get('label', '')[:200]})
    return selected, duplicates


def read_capture(path: Path) -> dict:
    """Verify the manifest before copying its source into the workbench."""
    if path.stat().st_size > 1024 * 1024:
        raise CaptureError('采集清单过大。')
    data = json.loads(path.read_text(encoding='utf-8'))
    data['url'] = detail_url(data['url'])
    screenshot = Path(data['screenshot']).resolve()
    if screenshot.parent != path.parent.resolve() or screenshot.name != 'job-evidence.png':
        raise CaptureError('采集图片不在对应清单目录中。')
    if (not isinstance(data['text'], str) or not data['text'].strip()
            or len(data['text']) > 100000 or data.get('sent') is not False
            or not data.get('captured_at')):
        raise CaptureError('采集清单缺少有效来源记录。')
    if screenshot.stat().st_size > 20 * 1024 * 1024:
        raise CaptureError('采集图片过大。')
    if (hashlib.sha256(screenshot.read_bytes()).hexdigest() != data.get('screenshot_sha256')
            or hashlib.sha256(data['text'].encode('utf-8')).hexdigest() != data.get('text_sha256')):
        raise CaptureError('采集证据校验失败，未导入。')
    return data


def wait_for_capture(process, timeout: float, cancelled, on_tick=lambda: None):
    """Wait without blocking the GUI, and always reap a cancelled/failed child."""
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled():
                return '', 'cancelled'
            if time.monotonic() >= deadline:
                return '', 'timeout'
            try:
                output, _ = process.communicate(timeout=.3)
                return output, ''
            except subprocess.TimeoutExpired:
                on_tick()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


class BrowserSession:
    """A single visible browser window bound to a selected BOSS document."""
    def __init__(self):
        from pywinauto import Desktop
        self.desktop = Desktop
        candidates = []
        for window in Desktop(backend='win32').windows(visible_only=True):
            if (window.class_name() != 'Chrome_WidgetWin_1' or window.is_minimized()
                    or not _browser_process(window.process_id())):
                continue
            for document in Desktop(backend='uia').window(handle=window.handle).descendants(control_type='Document'):
                try:
                    value = page_url(str(document.iface_value.CurrentValue))
                    bounds = rect(document)
                    if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                        candidates.append((window, document, value))
                except Exception:
                    continue
        if len(candidates) != 1:
            raise CaptureError('请只保留一个显示 BOSS 搜索列表或详情的浏览器窗口，其余最小化。')
        self.window, self.document, self.expected = candidates[0]
        self.origin = self.expected
        self.origin_value = str(self.document.iface_value.CurrentValue)

    def current(self):
        if not self.window.is_visible() or self.window.is_minimized():
            raise CaptureStopped('浏览器已关闭或最小化，自动采集停止。')
        docs = []
        for document in self.desktop(backend='uia').window(handle=self.window.handle).descendants(control_type='Document'):
            try:
                value = str(document.iface_value.CurrentValue)
                bounds = rect(document)
                if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                    docs.append((document, value))
            except Exception:
                continue
        if not docs:
            raise PageLoading('当前页面尚未加载完成，未采集。')
        if len(docs) != 1:
            raise CaptureStopped('无法唯一确认浏览器当前页面，自动采集停止。')
        return docs[0]

    def guard(self):
        try:
            document, value = self.current()
        except PageLoading:
            raise CaptureStopped('当前页面在采集期间消失或仍在加载，已停止自动采集。') from None
        if (page_url(value) != self.expected
                or ('/web/geek/jobs' in self.expected and value != self.origin_value)):
            raise CaptureStopped('当前标签页已切换或发生跳转，自动采集停止。')
        for node in document.descendants(control_type='Text'):
            if contained(rect(node), rect(document)) and RESTRICTED.search(node.window_text()):
                raise CaptureStopped('页面出现登录、验证或访问限制，自动采集停止；不会自动处理。')
        return document

    def scan_candidates(self, known: set[str], limit: int, screens: int):
        """Read visible cards before each scroll; never synthesize next-page URLs."""
        if type(screens) is not int or not 1 <= screens <= 20:
            raise CaptureError('列表翻页上限必须为 1～20 屏。')
        if type(limit) is not int or not 1 <= limit <= MAX_JOBS:
            raise CaptureError('每轮采集数量必须为 1～5。')
        if '/job_detail/' in self.expected or screens == 1:
            return self.candidates(known, limit)
        result, seen, old = [], set(), set()
        self.scan_steps = []
        previous_signature = None
        for step in range(screens):
            document = self.guard()
            rows = []
            for node in document.descendants(control_type='Hyperlink'):
                if node.class_name() == 'job-name' and contained(rect(node), rect(document)):
                    try:
                        rows.append({'url': detail_url(str(node.iface_value.CurrentValue)),
                                     'label': node.window_text()[:200]})
                    except CaptureError:
                        continue
            signature = tuple(r['url'] for r in rows)
            self.scan_steps.append({'screen': step + 1, 'visible': len(rows)})
            for row in rows:
                if row['url'] in known:
                    old.add(row['url'])
                elif row['url'] not in seen and len(result) < limit:
                    result.append(row)
                seen.add(row['url'])
            if len(result) >= limit or step + 1 >= screens:
                break
            if signature == previous_signature:
                self.scan_steps[-1]['stop'] = 'no_progress'
                break
            previous_signature = signature
            self.guard()
            self.window.set_focus()
            from pywinauto.uia_defines import NoPatternInterfaceError
            try:scroll = document.iface_scroll
            except NoPatternInterfaceError:scroll=None
            if scroll and scroll.CurrentVerticallyScrollable and scroll.CurrentVerticalScrollPercent < 99.9:
                document.scroll('down', 'page', count=1)
            else:
                next_buttons = [n for n in document.descendants()
                                if n.element_info.control_type in {'Hyperlink', 'Button'}
                                and n.window_text().strip() == '下一页'
                                and n.is_enabled() and contained(rect(n), rect(document))]
                if not next_buttons:
                    self.scan_steps[-1]['stop'] = 'end_of_list'
                    break
                if len(next_buttons) != 1:
                    raise CaptureStopped('下一页按钮不唯一，已停止。')
                before_url = self.origin_value
                if 'disabled' in next_buttons[0].class_name().split():
                    self.scan_steps[-1]['stop']='end_of_list';break
                next_buttons[0].iface_invoke.Invoke()
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    try:next_doc,value=self.current()
                    except PageLoading:
                        time.sleep(.1);continue
                    if page_url(value) != self.origin:
                        raise CaptureStopped('翻页跳转至非搜索列表，已停止。')
                    # Only page-number changes are allowed; filters retain blank values too.
                    from urllib.parse import parse_qsl
                    def filters(url):
                        return sorted((k,v) for k,v in parse_qsl(urlsplit(url).query,keep_blank_values=True) if k not in {'page','pageNo'})
                    if filters(value) != filters(before_url):
                        raise CaptureStopped('翻页过程中搜索条件发生变化，已停止。')
                    self.origin_value=value
                    self.guard()
                    next_urls=[]
                    for n in next_doc.descendants(control_type='Hyperlink'):
                        if n.class_name()=='job-name' and contained(rect(n),rect(next_doc)):
                            try:next_urls.append(detail_url(str(n.iface_value.CurrentValue)))
                            except CaptureError:pass
                    if next_urls and tuple(next_urls)!=signature:break
                    time.sleep(.15)
                else:
                    self.scan_steps[-1]['stop']='next_page_no_progress';break
            time.sleep(.6)
            self.guard()
        return result, len(old)

    def candidates(self, known: set[str], limit: int):
        document = self.guard()
        if '/job_detail/' in self.expected:
            return ([] if self.expected in known else [{'url': self.expected, 'label': ''}], int(self.expected in known))
        records = []
        for node in document.descendants(control_type='Hyperlink'):
            if node.class_name() != 'job-name':
                continue
            try:
                records.append({'url': str(node.iface_value.CurrentValue), 'class': node.class_name(),
                                'rect': rect(node), 'label': node.window_text()})
            except Exception:
                continue
        selected, duplicates = select_candidates(records, rect(document), known, limit)
        if not records:
            raise CaptureError('当前搜索列表还没有可读取的岗位链接，请等页面加载完成。')
        return selected, duplicates

    def open_detail(self, url: str):
        target = detail_url(url)
        previous = self.expected
        self.guard()
        self.window.set_focus()
        if target != self.expected:
            ui = self.desktop(backend='uia').window(handle=self.window.handle)
            bars = [n for n in ui.descendants(control_type='Edit')
                    if n.class_name() == 'OmniboxViewViews' and n.is_visible()
                    and n.window_text().startswith('https://')]
            if len(bars) != 1:
                raise CaptureStopped('无法唯一定位浏览器地址栏，停止自动打开岗位。')
            # Revalidate immediately before changing the address, never type in page forms.
            self.guard()
            bars[0].set_edit_text(target)
            bars[0].type_keys('{ENTER}', set_foreground=True)
            self.expected = target
        deadline = time.monotonic() + 15
        stable_since = None
        while time.monotonic() < deadline:
            try:
                document, value = self.current()
            except PageLoading:
                time.sleep(.2)
                continue
            current = page_url(value)
            if current == target:
                self.guard()
                titles = [n for n in document.descendants() if n.class_name() == 'info-primary']
                if titles:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    if time.monotonic() - stable_since >= .7:
                        break
                else:
                    stable_since = None
            elif current != previous:
                raise CaptureStopped('打开岗位时跳转到非目标页面，自动采集停止。')
            else:
                stable_since = None
            time.sleep(.2)
        else:
            raise CaptureStopped('岗位详情加载超时，自动采集停止。')
        # Browser keyboard navigation only; no page buttons or chat controls are invoked.
        self.window.type_keys('^{HOME}', set_foreground=True)
        time.sleep(.25)
        self.guard()

    def capture(self, directory: Path):
        self.guard()
        result = capture_current(directory, expected_handle=self.window.handle, expected_url=self.expected)
        self.guard()
        return result


def collect(directory: Path, session, known: set[str], limit: int, screens: int = 1) -> dict:
    if type(limit) is not int or not 1 <= limit <= MAX_JOBS:
        raise CaptureError('每轮采集数量必须为 1～5。')
    if type(screens) is not int or not 1 <= screens <= 20:
        raise CaptureError('列表翻页上限必须为 1～20 屏。')
    report = {'status': 'running', 'sent': False, 'started_at': datetime.now(timezone.utc).isoformat(),
              'limit': limit, 'captures': [], 'failures': [], 'known_skipped': 0,
              'scope': 'bounded_list_pagination' if screens > 1 else 'current_visible_results_only',
              'max_screens': screens, 'source_page': session.origin}
    directory.mkdir(parents=True, exist_ok=True)
    def save():
        atomic_write(directory / 'batch.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
    save()
    try:
        candidates, report['known_skipped'] = (session.scan_candidates(known, limit, screens)
                                               if screens > 1 else session.candidates(known, limit))
        report['candidate_count'] = len(candidates)
        report['scan_steps'] = getattr(session, 'scan_steps', [])
        save()
        for index, candidate in enumerate(candidates):
            report['progress'] = f'正在采集第 {index + 1} / {len(candidates)} 个岗位。'
            save()
            url = detail_url(candidate['url'])
            try:
                session.open_detail(url)
                target = directory / f'job-{index+1}'
                result = session.capture(target)
                if detail_url(result['url']) != url:
                    raise CaptureStopped('采集结果与目标链接不一致，已停止。')
                verified = read_capture(target / 'capture.json')
                if verified['url'] != url:
                    raise CaptureStopped('采集清单与目标链接不一致，已停止。')
                report['captures'].append({'url': url, 'manifest': str((target / 'capture.json').resolve())})
            except CaptureStopped:
                raise
            except CaptureError as exc:
                report['failures'].append({'url': url, 'reason': str(exc)})
            save()
        report['status'] = 'completed'
    except CaptureError as exc:
        report['status'] = 'stopped'
        report['reason'] = str(exc)
    except Exception:
        report['status'] = 'stopped'
        report['reason'] = '浏览器状态或采集过程异常，已停止；已完成的证据仍保留。'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description='Capture current visible BOSS results, without sending.')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--known-file', type=Path, required=True)
    parser.add_argument('--screens', type=int, default=1)
    parser.add_argument('--private', type=Path, default=PRIVATE)
    args = parser.parse_args()
    try:
        known = set(json.loads(args.known_file.read_text(encoding='utf-8')))
        with browser_session_lock(args.private):
            report = collect(args.output_dir, BrowserSession(), known, args.limit, args.screens)
        print(json.dumps({'ok': True, 'status': report['status']}, ensure_ascii=False))
        return 0
    except (CaptureError, BrowserSessionBusy) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({'ok': False, 'error': '无法启动浏览器采集，请打开一个 BOSS 搜索列表后再试。'}, ensure_ascii=False))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
