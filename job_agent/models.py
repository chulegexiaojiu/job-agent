"""Validated model outputs and the user-authorized, local-only review policy."""
from __future__ import annotations

import re
from datetime import date, timedelta
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Evidence(StrictModel):
    value: str
    quote: str


class JobExtraction(StrictModel):
    title: Evidence
    company: Evidence
    location: Evidence
    company_size: Evidence
    employment: Evidence
    industry: Evidence
    salary: Evidence
    responsibilities: list[Evidence]
    requirements: list[Evidence]


class ResearchClaim(StrictModel):
    topic: str
    claim: str
    evidence_quote: str
    source_url: str
    source_date: str


class ResearchBrief(StrictModel):
    identity_summary: str
    identity_confirmed: bool
    claims: list[ResearchClaim]
    uncertainties: list[str]
    risks: list[str]


class ClaimVerdict(StrictModel):
    claim_index: int
    supported: bool
    reason: str


class IdentityEvidence(StrictModel):
    source_url: str
    evidence_quote: str


class ClaimReview(StrictModel):
    identity_supported: bool
    identity_reason: str
    identity_evidence: list[IdentityEvidence]
    verdicts: list[ClaimVerdict]


class DimensionScore(StrictModel):
    score: int = Field(ge=0, le=100)
    reason: str
    source_urls: list[str]


class Assessment(StrictModel):
    skill_growth: DimensionScore
    business_outlook: DimensionScore
    company_background: DimensionScore
    career_recognition: DimensionScore
    personal_match: DimensionScore
    gaps: list[str]
    recommendation: str
    selected_fact_ids: list[str]


WEIGHTS = {'skill_growth': 0.4, 'business_outlook': 0.3,
           'company_background': 0.2, 'career_recognition': 0.1}
DAILY_CONTACT_LIMIT = 120


def arrival_date(application_date: date | None = None) -> str:
    return ((application_date or date.today()) + timedelta(days=14)).isoformat()


def canonical(value: str) -> str:
    return re.sub(r'[\s\u3000\-—–·・_／/｜|,，。:：()（）]+', '', value).casefold()


def employee_range(value: str) -> tuple[int, int] | None:
    """Only accept bounded, explicit counts; don't guess from 'small' or '500+'."""
    value = value.replace(',', '').replace('，', '').strip().lower()
    match = re.fullmatch(r'(\d+)\s*[-—–~～至到]\s*(\d+)\s*(?:人|名|employees)?', value)
    if match:
        low, high = map(int, match.groups())
        return (low, high) if 0 < low <= high else None
    match = re.fullmatch(r'(\d+)\s*(?:人|名|employees)', value)
    if match:
        number = int(match.group(1))
        return (number, number) if number > 0 else None
    return None


def check_text_evidence(job: JobExtraction, source: str) -> None:
    normalized = re.sub(r'\s+', '', source)
    for name in JobExtraction.model_fields:
        entries = getattr(job, name)
        for evidence in entries if isinstance(entries, list) else [entries]:
            if not evidence.value:
                if evidence.quote:
                    raise ValueError('空字段不应包含引用。')
                continue
            quote = re.sub(r'\s+', '', evidence.quote)
            if not quote or quote not in normalized:
                raise ValueError('岗位提取引用未在原文中找到：' + name)
            if name not in {'responsibilities', 'requirements'} and canonical(evidence.value) not in canonical(evidence.quote):
                raise ValueError('岗位提取值不在其引用内：' + name)


def crosscheck(text: JobExtraction, image: JobExtraction | None) -> dict:
    conflicts, missing, matches = [], [], []
    if image is None:
        return {'passed': False, 'matches': [], 'conflicts': [], 'missing': ['岗位截图']}
    for name in ['title', 'company', 'location', 'company_size']:
        left, right = getattr(text, name).value, getattr(image, name).value
        if not left or not right:
            missing.append(name)
        elif name in {'title', 'company'} and any(re.search(r'\.{3,}|…|⋯', value) for value in (left, right)):
            missing.append(name + '（名称被截断）')
        elif name == 'company_size' and employee_range(left) and employee_range(left) == employee_range(right):
            matches.append(name)
        elif canonical(left) == canonical(right):
            matches.append(name)
        else:
            conflicts.append({'field': name, 'text': left, 'image': right})
    return {'passed': not conflicts and not missing, 'matches': matches,
            'conflicts': conflicts, 'missing': missing}


def eligibility(job: JobExtraction, comparison: dict) -> list[str]:
    reasons = []
    if not comparison['passed']:
        reasons.append('截图与网页文字尚未通过关键信息交叉核验。')
    count = employee_range(job.company_size.value)
    if count is None:
        reasons.append('无法确认公司人数范围，暂停自动触达。')
    elif count[0] < 20 or count[1] > 500:
        reasons.append('公司人数范围未完全落在 20～500 人。')
    stage = job.title.value + ' ' + job.employment.value
    if not re.search(r'实习|校招|应届|intern|graduate|campus', stage, re.I):
        reasons.append('未明确为实习或校招岗位。')
    industry = ' '.join([job.title.value, job.industry.value] + [x.value for x in job.responsibilities])
    if not re.search(r'人工智能|大模型|机器学习|深度学习|生成式|智能体|(?<![A-Za-z])(?:AI|RAG|LLM|Agents?)(?![A-Za-z])', industry, re.I):
        reasons.append('未找到明确的 AI 相关业务依据。')
    return reasons


def quality_score(assessment: Assessment) -> float:
    return round(sum(getattr(assessment, name).score * weight for name, weight in WEIGHTS.items()), 1)
