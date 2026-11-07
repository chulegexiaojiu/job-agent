"""Local document ingestion, contact minimization and source-addressable facts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from job_agent.settings import PRIVATE, atomic_write
from job_agent.store import Store

HEADINGS = {'个人概况', '核心能力', '项目经历', '教育背景', '技术栈', '自我评价',
            '工作经历', '实习经历', '技能', '教育经历', '工作经验', '项目经验'}
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)')
URL = re.compile(r'https?://[^\s<>]+')


def redact_contacts(text: str, identity: dict | None = None) -> str:
    for name in ['name', 'email', 'phone', 'wechat']:
        value = (identity or {}).get(name, '')
        if isinstance(value, str) and value:
            text = text.replace(value, '[个人信息已隐藏]')
    text = EMAIL.sub('[邮箱已隐藏]', text)
    text = PHONE.sub('[电话已隐藏]', text)
    text = URL.sub('[个人链接已隐藏]', text)
    text = re.sub(r'(?:微信|WeChat|QQ)\s*[:：]?\s*[a-zA-Z0-9_-]+', '[社交账号已隐藏]', text, flags=re.I)
    return text


def extract_document(path: Path) -> list[tuple[str, str]]:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('素材超过 20 MB，请先拆分。')
    if path.suffix.lower() == '.docx':
        with ZipFile(path) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > 10 * 1024 * 1024:
                raise ValueError('Word 正文过大。')
            xml = archive.read(info)
        if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
            raise ValueError('Word 文件包含不支持的 XML 声明。')
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        root = ET.fromstring(xml)
        result = [('paragraph:' + str(index), ''.join(node.text or '' for node in paragraph.findall('.//w:t', ns)).strip())
                  for index, paragraph in enumerate(root.findall('.//w:p', ns))]
    elif path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('暂不读取加密 PDF，请导出未加密副本。')
        if len(reader.pages) > 100:
            raise ValueError('PDF 超过 100 页，请先拆分素材。')
        result = [('page:' + str(index + 1), page.extract_text() or '') for index, page in enumerate(reader.pages)]
        if any(len(text.strip()) < 15 for _, text in result):
            raise ValueError('PDF 存在无文字层或文字过少的页面，请先 OCR 或提供 Word；未将空页当作成功。')
    elif path.suffix.lower() in {'.txt', '.md'}:
        result = [('line:' + str(index + 1), text.strip()) for index, text in enumerate(path.read_text(encoding='utf-8-sig').splitlines())]
    else:
        raise ValueError('支持 DOCX、带文字层 PDF、TXT 和 Markdown。')
    result = [(locator, text) for locator, text in result if text]
    if not result or sum(len(text) for _, text in result) > 150000:
        raise ValueError('素材为空或文字超过 150000 字符，请拆分。')
    return result


def read_identity(private: Path = PRIVATE) -> dict:
    path = private / 'identity.json'
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8-sig'))


def ingest(path: Path, store: Store, private: Path = PRIVATE) -> int:
    lines = extract_document(path)
    identity = read_identity(private)
    text = '\n'.join(text for _, text in lines)
    first = lines[0][1]
    if not identity.get('name') and re.fullmatch(r'[\u4e00-\u9fff]{2,6}', first) and first not in HEADINGS:
        identity['name'] = first
    if not identity.get('phone') and (match := PHONE.search(text)):
        identity['phone'] = match.group(0)
    if not identity.get('email') and (match := EMAIL.search(text)):
        identity['email'] = match.group(0)
    if identity:
        atomic_write(private / 'identity.json', json.dumps(identity, ensure_ascii=False, indent=2).encode())
    facts, section = [], '其他素材'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for locator, original in lines:
        if original == identity.get('name') or (('电话' in original or '邮箱' in original or '微信' in original) and len(original) < 250):
            continue
        if original in HEADINGS:
            section = original
            continue
        if original.startswith('求职方向'):
            continue
        if section.startswith(('项目经历', '项目经验', '实习经历', '工作经历')) and re.search(r'\d{4}[.年/-]\d{1,2}', original) and len(original) < 160:
            section = section.split(' / ')[0] + ' / ' + redact_contacts(original, identity)
            continue
        cleaned = redact_contacts(original, identity)
        fact_id = hashlib.sha256((digest + ':' + locator + ':' + cleaned).encode()).hexdigest()[:20]
        facts.append({'id': fact_id, 'source': str(path.resolve()), 'locator': locator, 'section': section, 'text': cleaned})
    return store.add_facts(facts)


def cloud_facts(facts: list[dict]) -> list[dict]:
    """Never transmit source filenames, local paths, contacts, or unconfirmed rows."""
    return [{'id': fact['id'], 'section': fact['section'], 'text': fact['text']}
            for fact in facts if fact['confirmed']]
