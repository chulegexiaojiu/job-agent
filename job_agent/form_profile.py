"""Strict local profile schema for a reviewed, single-page external form."""
from __future__ import annotations
import re
from urllib.parse import urlsplit

from job_agent.delivery import DeliveryBlocked


def validate_profile(data):
    required={'id','url','company','title','reviewed','identity_source_url','identity_evidence_quote',
              'company_selector','title_selector','fields','upload_selector','attachment_selector',
              'submit_selector','receipt_selector','receipt_prefix'}
    if not isinstance(data,dict) or set(data)!=required:
        raise DeliveryBlocked('表单配置字段不完整或包含未知字段。')
    if not isinstance(data['id'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',data['id']):
        raise DeliveryBlocked('表单配置 ID 无效。')
    for key in ('url','identity_source_url'):
        p=urlsplit(data[key])
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in {None,443}:
            raise DeliveryBlocked('表单与证据链接必须为 HTTPS。')
    for key in ('company','title','identity_evidence_quote','receipt_prefix'):
        if not isinstance(data[key],str) or not data[key].strip() or len(data[key])>2000:
            raise DeliveryBlocked('表单配置文字字段无效。')
    if type(data['reviewed']) is not bool:raise DeliveryBlocked('配置审核标志无效。')
    def selector(value):
        if not isinstance(value,dict) or not value or set(value)-{'type','name','class','id'}:
            raise DeliveryBlocked('表单控件选择器无效。')
        if not any(value.get(k) for k in ('name','class','id')):
            raise DeliveryBlocked('控件需要明确的名称、类名或 ID。')
        if any(not isinstance(v,str) or len(v)>300 for v in value.values()):
            raise DeliveryBlocked('控件选择器内容无效。')
    for key in required:
        if key.endswith('_selector'):selector(data[key])
    if not isinstance(data['fields'],dict) or not data['fields'] or set(data['fields'])-{'name','email','phone','arrival_date','days_per_week','duration_months'}:
        raise DeliveryBlocked('仅支持已确认身份与到岗安排字段。')
    for value in data['fields'].values():selector(value)
    return data
