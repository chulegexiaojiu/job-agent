"""Isolated per-job review workflow. There is deliberately no send or click adapter."""
from __future__ import annotations

from pathlib import Path

from job_agent.client import Cancelled, ModelClient, image_data
from job_agent.exporter import export_draft
from job_agent.materials import cloud_facts, read_identity, redact_contacts
from job_agent.models import check_text_evidence, crosscheck, eligibility, quality_score
from job_agent.settings import PRIVATE
from job_agent.store import Store
from tools.check_api import ProbeError


class Pipeline:
    def __init__(self, store: Store, client: ModelClient, private: Path = PRIVATE, on_progress=None):
        self.store, self.client, self.private = store, client, private
        self.on_progress = on_progress or (lambda job_id, message: None)

    def progress(self, job_id: str, message: str):
        self.store.event(job_id, 'progress', message)
        self.on_progress(job_id, message)

    def run(self, job_id: str) -> str:
        if not self.store.claim(job_id):
            return self.store.job(job_id)['status']
        detail = {'mode': 'dry_run', 'sent': False}
        try:
            row = self.store.job(job_id)
            previous = row['detail']
            if previous.get('source_revision'):
                detail['source_revision'] = previous['source_revision']
            if previous.get('exports') or previous.get('stale_exports'):
                detail['stale_exports'] = previous.get('exports') or previous['stale_exports']
            self.store.update(job_id, quality=None, match=None, detail=detail)
            self.client.checkpoint()
            self.progress(job_id, '独立提取网页文字与截图，核对公司、岗位、城市和人数。')
            identity = read_identity(self.private)
            text = redact_contacts(row['jd'], identity)
            job = self.client.extract_job(text=text)
            check_text_evidence(job, text)
            visual = None
            if row['screenshot']:
                visual = self.client.extract_job(image=image_data(Path(row['screenshot'])))
            comparison = crosscheck(job, visual)
            detail.update({'job': job.model_dump(), 'image_job': visual.model_dump() if visual else None,
                           'crosscheck': comparison})
            self.store.update(job_id, title=job.title.value, company=job.company.value, detail=detail)
            reasons = eligibility(job, comparison)
            if reasons:
                detail['pause_reasons'] = reasons
                self.store.update(job_id, status='paused', detail=detail)
                self.progress(job_id, '已暂停：' + ' '.join(reasons))
                return 'paused'
            self.progress(job_id, '独立研究公司与工作内容；读取公开来源并逐字核对引用。')
            self.client.on_progress = lambda message: self.progress(job_id, message)
            research = self.client.research(job.company.value, job.title.value, job.location.value)
            detail['research'] = research
            if research['status'] != 'verified_sources':
                detail['pause_reasons'] = ['公司身份或研究来源尚未核实，不生成简历。']
                self.store.update(job_id, status='paused', detail=detail)
                self.progress(job_id, detail['pause_reasons'][0])
                return 'paused'
            confirmed = self.store.facts(confirmed_only=True)
            if not confirmed:
                detail['pause_reasons'] = ['公司研究完成；请先确认真实素材，再继续简历定制。']
                self.store.update(job_id, status='facts_required', detail=detail)
                self.progress(job_id, detail['pause_reasons'][0])
                return 'facts_required'
            self.progress(job_id, '分别评估岗位质量和个人匹配度，选择相关真实素材。')
            assessment = self.client.assess(job.model_dump(), research, cloud_facts(confirmed))
            self.client.checkpoint()
            detail['assessment'] = assessment.model_dump()
            # Recheck confirmations immediately before exporting in case user revoked one during analysis.
            current = self.store.facts(confirmed_only=True)
            export = export_draft(self.private / 'jobs' / job_id, job.model_dump(), current,
                                  assessment.selected_fact_ids, identity)
            detail['exports'] = export
            detail['export_fact_ids'] = list(dict.fromkeys(assessment.selected_fact_ids + [fact['id'] for fact in current if '教育' in fact['section']]))
            detail['pause_reasons'] = []
            self.store.update(job_id, status='draft_ready', quality=quality_score(assessment),
                              match=assessment.personal_match.score, detail=detail)
            self.progress(job_id, '定制草稿已生成，已保存素材来源；没有发送或提交。')
            return 'draft_ready'
        except Cancelled:
            self.store.update(job_id, status='cancelled', detail=detail)
            self.progress(job_id, '已停止该岗位的后续操作。')
            return 'cancelled'
        except ProbeError as exc:
            message = ('API 返回 HTTP ' + str(exc.status)) if exc.status else 'API 响应未完成或网络超时'
            detail['error'] = message
            self.store.update(job_id, status='error', detail=detail)
            self.progress(job_id, message)
            if exc.status in {401, 403, 429}:
                raise
            return 'error'
        except Exception:
            # Untrusted output and exception messages may contain private materials; don't log them.
            detail['error'] = '解析、证据核验或本地导出失败；未生成通过结论。'
            self.store.update(job_id, status='error', detail=detail)
            self.progress(job_id, detail['error'])
            return 'error'
