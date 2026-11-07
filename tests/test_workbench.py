"""Deterministic workflow checks using synthetic materials; no external model calls."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from docx import Document
from pydantic import ValidationError

from job_agent.app import Workbench, single_instance
from job_agent.client import Cancelled, ModelClient
from job_agent.exporter import export_draft
from job_agent.materials import cloud_facts, ingest, redact_contacts
from job_agent.models import (Assessment, JobExtraction, ResearchBrief, arrival_date, check_text_evidence,
                              crosscheck, eligibility, employee_range, quality_score)
from job_agent.pipeline import Pipeline
from job_agent.settings import Connection, atomic_write
from job_agent.sources import SourceError, fetch_public_text, public_url, quote_present, resolve_public
from job_agent.store import Store, canonical_job_url
from tools.check_api import ProbeError


def job_fixture():
    values = {'title': 'AI 应用开发实习生', 'company': '示例人工智能公司', 'location': '远程',
              'company_size': '20-99人', 'employment': '实习', 'industry': '人工智能', 'salary': ''}
    data = {name: {'value': value, 'quote': value} for name, value in values.items()}
    data['requirements'] = [{'value': 'Python', 'quote': 'Python'}]
    data['responsibilities'] = [{'value': '开发 RAG 系统', 'quote': '开发 RAG 系统'}]
    return JobExtraction.model_validate(data)


def research_fixture():
    url = 'https://example.test/company'
    return {'status': 'verified_sources', 'brief': {'identity_summary': '合成公司用于测试', 'identity_confirmed': True,
            'claims': [{'topic': '产品', 'claim': 'AI 测试产品', 'evidence_quote': '合成公司开发人工智能测试产品', 'source_url': url, 'source_date': ''}],
            'uncertainties': [], 'risks': []}, 'pages': [], 'failed': []}


def assessment_fixture(ids):
    score = {'score': 70, 'reason': '基于测试资料的评分', 'source_urls': ['https://example.test/company']}
    return Assessment.model_validate({**{name: score for name in ['skill_growth', 'business_outlook', 'company_background', 'career_recognition', 'personal_match']},
                                      'gaps': ['缺少生产经验'], 'recommendation': '仅生成测试草稿', 'selected_fact_ids': ids})


class RulesTests(unittest.TestCase):
    def test_employee_ranges_require_bounds_within_policy(self):
        for source, expected in [('20-99人', (20, 99)), ('100～499 人', (100, 499)), ('20人', (20, 20)), ('500+', None), ('少于20人', None), ('10000-999人', None), ('未知', None)]:
            self.assertEqual(employee_range(source), expected)
        job = job_fixture()
        self.assertEqual(eligibility(job, crosscheck(job, job)), [])
        job.company_size.value = '20-999人'
        self.assertTrue(any('20～500' in reason for reason in eligibility(job, crosscheck(job, job))))

    def test_image_conflict_and_missing_image_pause(self):
        left, right = job_fixture(), job_fixture()
        right.company.value = '另一家公司'
        self.assertFalse(crosscheck(left, right)['passed'])
        self.assertEqual(crosscheck(left, right)['conflicts'][0]['field'], 'company')
        self.assertFalse(crosscheck(left, None)['passed'])

    def test_matching_truncated_names_do_not_pass_crosscheck(self):
        for name in ['company', 'title']:
            for ellipsis in ['...', '…', '⋯']:
                job = job_fixture()
                getattr(job, name).value += ellipsis
                result = crosscheck(job, job)
                self.assertFalse(result['passed'])
                self.assertIn(name + '（名称被截断）', result['missing'])

    def test_hallucinated_quote_is_rejected(self):
        job = job_fixture()
        source = '\n'.join(e['quote'] for name, e in job.model_dump().items() if isinstance(e, dict)) + '\nPython\n开发 RAG 系统'
        check_text_evidence(job, source)
        job.company.quote = '并不存在的公司名'
        with self.assertRaises(ValueError):
            check_text_evidence(job, source)

    def test_arrival_date_uses_actual_application_day(self):
        self.assertEqual(arrival_date(date(2026, 12, 25)), '2027-01-08')
        self.assertEqual(arrival_date(date(2028, 2, 20)), '2028-03-05')

    def test_scores_are_separate_and_strict(self):
        result = assessment_fixture(['fact'])
        result.skill_growth.score = 90
        result.personal_match.score = 15
        self.assertEqual(quality_score(result), 78)
        data = result.model_dump()
        data['skill_growth']['score'] = True
        with self.assertRaises(ValidationError):
            Assessment.model_validate(data)


class SourceTests(unittest.TestCase):
    def test_private_urls_are_rejected(self):
        for url in ['file:///etc/passwd', 'https://localhost/', 'http://127.0.0.1/', 'http://169.254.169.254/', 'https://10.0.0.1/', 'https://[::1]/', 'https://u:p@example.test/', 'https://example.test:8443']:
            with self.subTest(url=url), self.assertRaises(SourceError):
                public_url(url)

    def test_dns_private_target_is_rejected_before_connect(self):
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.5', 443))]):
            with self.assertRaises(SourceError):
                resolve_public('public-name.test', 443)

    def test_quote_must_be_contained_and_long_enough(self):
        self.assertTrue(quote_present('This company builds AI tools', 'About us\nThis company builds AI tools for work.'))
        self.assertFalse(quote_present('AI', 'AI company'))
        self.assertFalse(quote_present('The company raised millions', 'The company did not raise money.'))


class StateAndMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name)
        self.store = Store(self.private / 'state.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_tracking_url_dedup_and_distinct_ids(self):
        first, inserted = self.store.add_job('https://jobs.test/position/123?utm_source=app', 'example')
        second, duplicate = self.store.add_job('https://jobs.test/position/123?from=app', 'example')
        third, distinct = self.store.add_job('https://jobs.test/position/124', 'example')
        self.assertTrue(inserted and distinct)
        self.assertFalse(duplicate)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_atomic_claim_and_restart_recovery(self):
        job, _ = self.store.add_job('https://jobs.test/1', 'example')
        self.assertTrue(self.store.claim(job))
        self.assertFalse(self.store.claim(job))
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.job(job)['status'], 'paused')

    def test_materials_remain_unconfirmed_and_private(self):
        doc = Document()
        for text in ['测试人', '电话：13800000000 ｜ 邮箱：fake@example.test', '教育背景', '合成大学｜本科 2023-2027',
                     '项目经历', '测试项目｜个人项目 2026.01—2026.03', '使用 Python 开发测试工具。']:
            doc.add_paragraph(text)
        path = self.private / 'private-person-name.docx'
        doc.save(path)
        self.assertEqual(ingest(path, self.store, self.private), 2)
        self.assertEqual(ingest(path, self.store, self.private), 0)
        facts = self.store.facts()
        self.assertFalse(any(f['confirmed'] for f in facts))
        self.assertEqual(cloud_facts(facts), [])
        self.store.confirm_facts([f['id'] for f in facts], True)
        payload = json.dumps(cloud_facts(self.store.facts()), ensure_ascii=False)
        self.assertNotIn('private-person-name', payload)
        self.assertNotIn('13800000000', payload)
        self.assertNotIn('fake@example.test', payload)
        self.assertIn('测试项目｜个人项目 2026.01—2026.03', payload)

    def test_export_only_exact_confirmed_materials(self):
        facts = [{'id': 'fact1', 'section': '项目经历 / 本地测试项目', 'text': '使用 Python 编写工具；未发布生产服务。',
                  'source': 'source.docx', 'locator': 'paragraph:5', 'confirmed': 1}]
        directory = self.private / 'draft'
        files = export_draft(directory, job_fixture().model_dump(), facts, ['fact1'], {'name': '测试人'})
        text = '\n'.join(p.text for p in Document(files['resume']).paragraphs)
        self.assertIn(facts[0]['text'], text)
        self.assertIn('示例人工智能公司', text)
        self.assertFalse(json.loads(Path(files['manifest']).read_text(encoding='utf-8'))['sent'])
        with self.assertRaises(ValueError):
            export_draft(directory, job_fixture().model_dump(), facts, ['invented-fact'], {'name': '测试人'})

    def test_export_keeps_projects_together_and_manifest_in_document_order(self):
        sections = ['项目经历 / 项目甲', '技术栈', '自我评价', '项目经历 / 项目乙', '教育背景', '核心能力']
        facts = [{'id': str(i), 'section': section, 'text': f'已确认素材{i}', 'source': 'synthetic.txt',
                  'locator': str(i), 'confirmed': 1} for i, section in enumerate(sections)]
        files = export_draft(self.private / 'ordered', job_fixture().model_dump(), facts,
                             [str(i) for i in range(len(facts))], {'name': '测试人'})
        doc = Document(files['resume'])
        headings = [p.text for p in doc.paragraphs if p.style.name == 'Heading 1']
        self.assertEqual(headings, ['教育背景', '核心能力', '项目经历 / 项目甲', '项目经历 / 项目乙', '技术栈', '自我评价'])
        manifest = json.loads(Path(files['manifest']).read_text(encoding='utf-8'))
        self.assertEqual([f['id'] for f in manifest['facts']], ['4', '5', '0', '3', '1', '2'])
        self.assertTrue(all(f['text'] in [p.text for p in doc.paragraphs] for f in facts))

    def test_second_window_lock_is_refused(self):
        with single_instance(self.private):
            with self.assertRaises(ValueError):
                with single_instance(self.private):
                    pass


class ResearchTests(unittest.TestCase):
    def test_bad_quote_is_removed_and_no_applicant_data_in_search(self):
        captured = []
        fixture = research_fixture()['brief']
        fixture['claims'][0]['evidence_quote'] = '模型捏造了一段根本不存在的原文'
        def transport(url, key, payload, timeout):
            captured.append(payload)
            if url.endswith('/responses'):
                return {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed', 'action': {'sources': [{'url': 'https://example.test/company'}]}}]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(fixture)}}]}
        client = ModelClient(Connection('https://api.test/v1', 'test'), 'synthetic-credential', transport=transport,
                             fetcher=lambda url: {'url': url, 'text': '合成公司开发人工智能测试产品，全部内容仅供自动化测试使用。'})
        result = client.research('合成公司', 'AI 实习', '远程')
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['brief']['claims'], [])
        self.assertNotIn('synthetic-credential', json.dumps(captured))
        self.assertIn('company', captured[0]['input'])

    def test_independent_review_rejects_partially_supported_claim(self):
        fixture = research_fixture()['brief']
        fixture['claims'][0]['claim'] = 'AI 公司拥有 200 位工程师并开发测试产品。'
        review = {'identity_supported': True, 'identity_reason': '合成测试身份',
                  'identity_evidence': [{'source_url': 'https://example.test/company', 'evidence_quote': '合成公司开发人工智能测试产品'}],
                  'verdicts': [{'claim_index': 0, 'supported': False, 'reason': '引文没有人数'}]}
        calls = []
        def transport(url, key, payload, timeout):
            calls.append(payload)
            if url.endswith('/responses'):
                return {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed', 'action': {'sources': [{'url': 'https://example.test/company'}]}}]}
            value = fixture if len(calls) == 2 else review
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
        client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', transport=transport,
                             fetcher=lambda url: {'url': url, 'text': '合成公司开发人工智能测试产品，全部内容仅供自动化测试使用。'})
        result = client.research('合成公司', 'AI实习生', '远程')
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['brief']['claims'], [])
        self.assertEqual(result['rejected_claims'][0]['reason'], '引文没有人数')
        self.assertEqual(len(calls), 3)

    def test_date_outside_claim_quote_is_rejected_even_if_review_accepts(self):
        fixture = research_fixture()['brief']
        fixture['claims'][0]['source_date'] = '2025-12-22'
        review = {'identity_supported': True, 'identity_reason': '合成身份',
                  'identity_evidence': [{'source_url': 'https://example.test/company',
                                         'evidence_quote': '合成公司开发人工智能测试产品'}],
                  'verdicts': [{'claim_index': 0, 'supported': True, 'reason': '错误地采用页面其他日期'}]}
        calls = []
        def transport(url, key, payload, timeout):
            calls.append(payload)
            if len(calls) == 1:
                return {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed',
                        'action': {'sources': [{'url': 'https://example.test/company'}]}}]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(fixture if len(calls) == 2 else review)}}]}
        client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', transport=transport,
                             fetcher=lambda url: {'url': url, 'text': '合成公司开发人工智能测试产品。发布日期2025-12-22。'})
        result = client.research('合成公司', 'AI实习生', '远程')
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['brief']['claims'], [])
        self.assertIn('日期', result['rejected_claims'][0]['reason'])

    def test_assessment_includes_confirmed_availability_without_identity(self):
        captured = []
        def transport(url, key, payload, timeout):
            captured.append(payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': assessment_fixture(['fact']).model_dump_json()}}]}
        client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', transport=transport)
        client.assess(job_fixture().model_dump(), research_fixture(), [{'id': 'fact', 'text': '使用Python'}])
        data = json.loads(captured[0]['messages'][1]['content'].split('\n', 1)[1])
        self.assertEqual(data['applicant_availability'], {'arrival_offset_days_after_application': 14,
                         'days_per_week_min': 5, 'days_per_week_max': 7, 'duration_months_min': 6})
        self.assertNotIn('name', data['applicant_availability'])

    def test_identity_review_receives_pages_and_requires_matching_quotes(self):
        source = 'https://example.test/company'
        page = '合成公司开发人工智能测试产品。公司主体为示例科技有限公司，地址为测试市一号。'
        for mode in ['valid', 'empty', 'invented_quote', 'unknown_url', 'identity_rejected']:
            with self.subTest(mode=mode):
                fixture = research_fixture()['brief']
                evidence = [{'source_url': source, 'evidence_quote': '公司主体为示例科技有限公司，地址为测试市一号。'}]
                if mode == 'empty':
                    evidence = []
                elif mode == 'invented_quote':
                    evidence[0]['evidence_quote'] = '另一家公司的不存在地址与主体'
                elif mode == 'unknown_url':
                    evidence[0]['source_url'] = 'https://unread.test/company'
                review = {'identity_supported': mode != 'identity_rejected', 'identity_reason': '测试理由',
                          'identity_evidence': evidence,
                          'verdicts': [{'claim_index': 0, 'supported': True, 'reason': '引文支持'}]}
                calls = []
                def transport(url, key, payload, timeout):
                    calls.append(payload)
                    if len(calls) == 1:
                        return {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed',
                                'action': {'sources': [{'url': source}]}}]}
                    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(fixture if len(calls) == 2 else review)}}]}
                client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', transport=transport,
                                     fetcher=lambda url: {'url': url, 'text': page})
                result = client.research('合成公司', 'AI实习生', '测试市')
                self.assertEqual(result['status'], 'verified_sources' if mode == 'valid' else 'unverified')
                payload_text = calls[2]['messages'][1]['content']
                self.assertIn('identity_pages', payload_text)
                self.assertIn(page, payload_text)
                self.assertEqual(len(result['brief']['claims']), 1)

    def test_image_extraction_requests_high_detail_for_both_protocols(self):
        for protocol in ['chat', 'responses']:
            captured = []
            text = job_fixture().model_dump_json()
            def transport(url, key, payload, timeout):
                captured.append(payload)
                if protocol == 'chat':
                    return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}
                return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}
            client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', protocol=protocol, transport=transport)
            client.extract_job(image='data:image/png;base64,TEST')
            content = captured[0]['messages'][1]['content'] if protocol == 'chat' else captured[0]['input'][0]['content']
            image = content[1]
            self.assertEqual(image['image_url']['detail'] if protocol == 'chat' else image['detail'], 'high')

    def test_no_fetchable_page_blocks_research(self):
        def transport(*args):
            return {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed', 'action': {'sources': [{'url': 'https://example.test/company'}]}}]}
        def blocked(url):
            raise SourceError('requires login')
        client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', transport=transport, fetcher=blocked)
        result = client.research('合成公司', 'AI 实习', '远程')
        self.assertEqual(result['status'], 'unverified')
        self.assertIsNone(result['brief'])

    def test_invented_source_or_fact_cannot_enter_assessment(self):
        result = assessment_fixture(['not-in-materials']).model_dump()
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        client = ModelClient(Connection('https://api.test/v1', 'test'), 'fake', transport=lambda *args: response)
        with self.assertRaises(ValueError):
            client.assess(job_fixture().model_dump(), research_fixture(), [{'id': 'actual-fact', 'text': 'Python'}])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name)
        self.store = Store(self.private / 'state.sqlite3')
        atomic_write(self.private / 'identity.json', json.dumps({'name': '测试人'}).encode())
        self.fact = {'id': 'fact1', 'source': 'local.docx', 'locator': 'paragraph:1', 'section': '技能', 'text': '使用 Python 开发工具。'}
        self.store.add_facts([self.fact])
        self.store.confirm_facts(['fact1'], True)
        jd = 'AI 应用开发实习生\n示例人工智能公司\n远程\n20-99人\n实习\n人工智能\nPython\n开发 RAG 系统'
        self.job_id, _ = self.store.add_job('https://jobs.test/1', jd, 'fixture.png')
        self.calls = []
        owner = self
        class FakeClient:
            def checkpoint(self):
                pass
            def extract_job(self, **kwargs):
                owner.calls.append('extract')
                return job_fixture()
            def research(self, *args):
                owner.calls.append('research')
                return research_fixture()
            def assess(self, *args):
                owner.calls.append('assess')
                return assessment_fixture(['fact1'])
        self.client = FakeClient()

    def tearDown(self):
        self.temp.cleanup()

    def run_pipeline(self):
        with patch('job_agent.pipeline.image_data', return_value='synthetic-image'):
            return Pipeline(self.store, self.client, self.private).run(self.job_id)

    def test_end_to_end_dry_run_research_before_resume(self):
        self.assertEqual(self.run_pipeline(), 'draft_ready')
        self.assertEqual(self.calls, ['extract', 'extract', 'research', 'assess'])
        row = self.store.job(self.job_id)
        self.assertFalse(row['detail']['sent'])
        self.assertTrue(Path(row['detail']['exports']['resume']).exists())
        self.assertEqual(row['quality'], 70)

    def test_source_revision_binding_survives_reanalysis(self):
        self.store.update(self.job_id,detail={'source_revision':'synthetic-revision'})
        self.assertEqual(self.run_pipeline(),'draft_ready')
        self.assertEqual(self.store.job(self.job_id)['detail']['source_revision'],'synthetic-revision')
        self.store.update(self.job_id,screenshot='')
        self.assertEqual(self.run_pipeline(),'paused')
        self.assertEqual(self.store.job(self.job_id)['detail']['source_revision'],'synthetic-revision')

    def test_missing_screenshot_blocks_research_and_draft(self):
        self.store.update(self.job_id, screenshot='')
        self.assertEqual(self.run_pipeline(), 'paused')
        self.assertNotIn('research', self.calls)
        self.assertNotIn('exports', self.store.job(self.job_id)['detail'])

    def test_unverified_research_blocks_resume(self):
        self.client.research = lambda *args: {'status': 'unverified', 'brief': None}
        self.assertEqual(self.run_pipeline(), 'paused')
        self.assertNotIn('assess', self.calls)

    def test_unconfirmed_facts_block_resume_after_research(self):
        self.store.confirm_facts(['fact1'], False)
        self.assertEqual(self.run_pipeline(), 'facts_required')
        self.assertIn('research', self.calls)
        self.assertNotIn('assess', self.calls)

    def test_revoking_used_fact_marks_existing_draft_stale(self):
        self.assertEqual(self.run_pipeline(), 'draft_ready')
        self.store.confirm_facts(['fact1'], False)
        row = self.store.job(self.job_id)
        self.assertEqual(row['status'], 'facts_required')
        self.assertNotIn('exports', row['detail'])
        self.assertIn('stale_exports', row['detail'])

    def test_failed_reanalysis_clears_scores_and_marks_previous_draft_stale(self):
        self.assertEqual(self.run_pipeline(), 'draft_ready')
        old_exports = self.store.job(self.job_id)['detail']['exports']
        self.store.update(self.job_id, screenshot='')
        self.assertEqual(self.run_pipeline(), 'paused')
        row = self.store.job(self.job_id)
        self.assertIsNone(row['quality'])
        self.assertIsNone(row['match'])
        self.assertNotIn('exports', row['detail'])
        self.assertEqual(row['detail']['stale_exports'], old_exports)

    def test_auth_or_quota_error_stops_batch(self):
        def reject(**kwargs):
            raise ProbeError('quota', 429)
        self.client.extract_job = reject
        with self.assertRaises(ProbeError):
            self.run_pipeline()
        self.assertEqual(self.store.job(self.job_id)['status'], 'error')


class WorkbenchWindowTests(unittest.TestCase):
    def test_render_empty_workbench_and_queue_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            app = Workbench(root, Path(directory))
            try:
                app.url.set('https://jobs.test/1')
                app.jd.insert('1.0', 'synthetic JD')
                app.add_job()
                root.update()
                self.assertEqual(len(app.store.jobs()), 1)
                self.assertIn('0 / 120', app.counts.get())
                self.assertEqual(app.store.jobs()[0]['status'], 'queued')
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
