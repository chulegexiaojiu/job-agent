"""Produce source-preserving, job-specific Word drafts and a local audit manifest."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import qn

from job_agent.models import arrival_date
from job_agent.settings import atomic_write
from job_agent.store import utc_now


def greeting_text(job: dict) -> str:
    return ('您好，我想申请贵公司的“' + job['title']['value'] + '”岗位。'
            '已根据岗位要求整理相关经历，方便进一步沟通吗？'
            '可于投递日起 14 天后到岗，每周 5～7 天，可实习 6 个月及以上。')


def export_draft(directory: Path, job: dict, facts: list[dict], selected_ids: list[str], identity: dict) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    available = {fact['id']: fact for fact in facts if fact['confirmed']}
    ids = list(dict.fromkeys(selected_ids))
    # Always retain confirmed education; prioritize other facts by relevance.
    education = [fact['id'] for fact in facts if fact['confirmed'] and '教育' in fact['section']]
    ids = list(dict.fromkeys(education + ids))
    if not ids or set(ids) - available.keys():
        raise ValueError('简历草稿仅能引用已确认存在的素材。')
    if not identity.get('name'):
        raise ValueError('未找到本地姓名，请补全个人资料后生成简历。')
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.65)
    section.left_margin = section.right_margin = Inches(0.7)
    normal = document.styles['Normal']
    normal.font.name = 'Calibri'
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    normal.paragraph_format.space_after = Pt(5)
    for style_name in ['Title', 'Heading 1', 'Heading 2']:
        style = document.styles[style_name]
        style.font.name = 'Calibri'
        style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        style.font.color.rgb = RGBColor.from_string('18364D')
    document.add_paragraph(identity['name'], 'Title')
    contacts = [identity.get(name, '') for name in ['phone', 'email']]
    document.add_paragraph('  |  '.join(value for value in contacts if value))
    document.add_paragraph('求职意向：' + job['title']['value'])
    document.add_paragraph('目标公司：' + job['company']['value'])
    document.add_paragraph('可到岗安排：投递日起 14 天后；每周 5～7 天；实习 6 个月及以上。')
    groups: dict[str, list[dict]] = OrderedDict()
    for fact_id in ids:
        fact = available[fact_id]
        groups.setdefault(fact['section'], []).append(fact)
    def section_rank(heading: str) -> int:
        if '教育' in heading:
            return 0
        if '核心' in heading:
            return 1
        if any(label in heading for label in ('实习', '工作经历', '项目经历')):
            return 2
        if any(label in heading for label in ('技术', '技能')):
            return 3
        if '自我' in heading:
            return 5
        return 4

    ordered_groups = sorted(groups.items(), key=lambda item: section_rank(item[0]))
    ids = [fact['id'] for _, entries in ordered_groups for fact in entries]
    for heading, entries in ordered_groups:
        document.add_heading(heading, level=1)
        for fact in entries:
            # Exact source wording prevents invented dates, metrics or accomplishments.
            document.add_paragraph(fact['text'])
    document.core_properties.author = ''
    document.core_properties.title = '岗位定制简历草稿'
    path = directory / 'resume-draft.docx'
    document.save(path)
    greeting = greeting_text(job)
    greeting_path = directory / 'greeting-draft.txt'
    atomic_write(greeting_path, greeting.encode('utf-8'))
    manifest = {'created_at': utc_now(), 'mode': 'dry_run', 'sent': False,
                'company': job['company']['value'], 'title': job['title']['value'],
                'arrival_if_applied_today': arrival_date(),
                'arrival_rule': 'Recompute actual application date + 14 days immediately before sending.',
                'wording_policy': 'Selected and reordered confirmed source wording; no new accomplishments.',
                'resume_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'facts': [{'id': fid, 'source': available[fid]['source'], 'locator': available[fid]['locator'],
                           'section': available[fid]['section'], 'text': available[fid]['text']} for fid in ids]}
    atomic_write(directory / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    return {'resume': str(path.resolve()), 'greeting': str(greeting_path.resolve()),
            'manifest': str((directory / 'manifest.json').resolve())}
