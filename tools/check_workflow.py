"""Opt-in live check with synthetic applicant facts and public company information."""
from __future__ import annotations

import json
from pathlib import Path
import sys

# Support direct CLI execution as well as python -m tools.check_workflow.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent.client import ModelClient, image_data
from job_agent.models import check_text_evidence, crosscheck, eligibility
from job_agent.exporter import export_draft
from job_agent.settings import PRIVATE, atomic_write
from job_agent.store import utc_now


def main():
    directory = PRIVATE / 'selfcheck'
    directory.mkdir(parents=True, exist_ok=True)
    report = {'started_at': utc_now(), 'synthetic_applicant_only': True, 'no_job_submission': True, 'status': 'running'}
    path = directory / 'workflow.json'
    atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2).encode())
    try:
        client = ModelClient.local()
        def progress(message):
            report['progress'] = message
            atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2).encode())
        client.on_progress = progress
        from PIL import Image, ImageDraw, ImageFont
        progress('生成并核对合成岗位的文字和截图；不使用真实招聘或个人素材。')
        lines = ['合成测试，不代表真实招聘信息', '示例人工智能公司', 'AI 应用开发实习生',
                 '工作地点：远程', '公司规模：20-99人', '招聘类型：实习', '所属行业：人工智能',
                 '工作内容：开发 RAG 系统', '任职要求：Python、向量检索、API 调试']
        image = Image.new('RGB', (1280, 720), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 32)
        for i, line in enumerate(lines):
            draw.text((30, 20 + i * 72), line, fill='black', font=font)
        image.save(directory / 'synthetic-job.png')
        text = '\n'.join(lines)
        extracted = client.extract_job(text=text)
        check_text_evidence(extracted, text)
        visual = client.extract_job(image=image_data(directory / 'synthetic-job.png'))
        comparison = crosscheck(extracted, visual)
        extraction = {'test_data': 'synthetic_not_a_real_job', 'text_extraction': extracted.model_dump(),
                      'image_extraction': visual.model_dump(), 'comparison': comparison}
        atomic_write(directory / 'extraction.json', json.dumps(extraction, ensure_ascii=False, indent=2).encode())
        report['text_image_crosscheck_passed'] = comparison['passed'] and not eligibility(extracted, comparison)
        if not report['text_image_crosscheck_passed']:
            raise ValueError('Synthetic extraction did not match.')
        research = client.research('Dify / LangGenius', 'AI application development intern (synthetic test role, not a vacancy)', 'Remote (test only)')
        atomic_write(directory / 'research.json', json.dumps(research, ensure_ascii=False, indent=2).encode())
        report['research_status'] = research['status']
        report['retrieved_pages'] = len(research['pages'])
        report['verified_quote_count'] = len((research.get('brief') or {}).get('claims', []))
        if research['status'] != 'verified_sources':
            report['status'] = 'paused_unverified_research'
        else:
            fact = {'id': 'synthetic-fact-only', 'section': '个人项目（合成自检，不是真实申请人）',
                    'text': '合成测试材料：使用 Python 完成一个本地文本检索练习。无企业实习或生产部署经历。',
                    'source': 'synthetic-fixture', 'locator': 'fixture:1', 'confirmed': 1}
            job = json.loads((directory / 'extraction.json').read_text(encoding='utf-8'))['text_extraction']
            job['company'] = {'value': 'Dify / LangGenius（合成测试，非真实招聘）', 'quote': 'Dify / LangGenius（合成测试，非真实招聘）'}
            assessment = client.assess(job, research, [{k: fact[k] for k in ['id', 'section', 'text']}])
            atomic_write(directory / 'assessment.json', assessment.model_dump_json(indent=2).encode())
            export_draft(directory / 'synthetic-draft', job, [fact], assessment.selected_fact_ids, {'name': '合成测试申请人'})
            report['status'] = 'pass'
            report['assessment_and_local_export'] = True
    except Exception as exc:
        report['status'] = 'error'
        report['error_type'] = type(exc).__name__
        report['http_status'] = getattr(exc, 'status', None)
    report['finished_at'] = utc_now()
    atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2).encode())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
