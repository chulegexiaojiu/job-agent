"""Durable outbound plans, quota reservations and evidence-based outcomes.

Creating/previewing a plan never sends. Execution is explicitly gated; unknown
outcomes retain their reservation and cannot be retried automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
from uuid import uuid4

from job_agent.models import JobExtraction, eligibility
from job_agent.settings import PRIVATE, atomic_write
from job_agent.store import Store, canonical_job_url, utc_now

def process_alive(pid: int | None) -> bool:
    if not pid:return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype=wintypes.HANDLE
        api.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()!=87
        try:
            code=wintypes.DWORD()
            return not api.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:api.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


KINDS = {'boss_greeting', 'resume_send', 'external_submit'}
TOUCHES = {'boss_greeting', 'external_submit'}
SENSITIVE = re.compile(r'身份证|护照|银行卡|支付|付款|缴费|保证金|人脸|验证码|承诺书|签署|password|passport|payment|ssn', re.I)


class DeliveryBlocked(ValueError):
    pass


class BatchStopped(DeliveryBlocked):
    pass


@dataclass(frozen=True)
class Policy:
    mode: str = 'dry_run'
    daily_new_jobs: int = 120
    daily_resume_sends: int = 120
    # Empty by default. Reviewed adapter IDs are local configuration, never model output.
    enabled_adapters: tuple[str, ...] = ()

    def validate(self):
        if self.mode not in {'dry_run', 'live'}:
            raise DeliveryBlocked('执行模式无效。')
        for value in (self.daily_new_jobs, self.daily_resume_sends):
            if type(value) is not int or not 1 <= value <= 120:
                raise DeliveryBlocked('每日限额必须为 1～120。')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(text: str) -> str:
    return re.sub(r'\s+', '', text).casefold()


def recent_reply_time(value: str) -> datetime:
    """Require an absolute timezone-aware message time, not the time it was reread."""
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None or not timedelta(0)<=datetime.now(timezone.utc)-stamp<=timedelta(days=7):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise DeliveryBlocked('回复缺少可核实的消息时间，或已超过 7 天／时间在未来；未建立自动发送依据。') from None
    return stamp


def classify_reply(text: str) -> str:
    """Conservative deterministic first gate; never treat a generic hello as consent."""
    if not text or len(text) > 10000:
        return 'needs_review'
    if SENSITIVE.search(text):
        return 'sensitive'
    if re.search(r'不合适|不考虑|暂不招聘|停止联系|不要再联系|别.{0,6}(?:简历|发)|不(?:要|用|需|必).{0,6}(?:简历|发)|暂.{0,4}不.{0,6}发|no thanks|do not send|don.t send', text, re.I):
        return 'declined'
    if re.search(r'如果|若|等.{0,12}(?:后|再)|之后|再说|待定|稍后|if\b|later\b',text,re.I):
        return 'needs_review'
    if re.search(r'https?://', text, re.I):
        wording=re.sub(r'https?://[^\s<>"\u3000]+','',text,flags=re.I)
        return 'external_link' if re.search(r'提交|投递|申请|网申|投简历|\bsubmit\b|\bapply\b',wording,re.I) else 'needs_review'
    if re.search(r'(?:请|可以|麻烦|方便|发|发送|提供|投递).{0,16}简历|简历.{0,12}(?:发|发送|给|提供)|(?:send|share).{0,25}(?:resume|cv)', text, re.I):
        return 'resume_requested'
    if re.search(r'面试|interview', text, re.I):
        return 'interview'
    return 'needs_review'


def validated_bundle(store: Store, private: Path, job_id: str) -> dict:
    row = store.job(job_id)
    d = row['detail']
    if row['status'] != 'draft_ready' or d.get('pause_reasons'):
        raise DeliveryBlocked('岗位没有有效定制草稿。')
    if eligibility(JobExtraction.model_validate(d['job']), d['crosscheck']):
        raise DeliveryBlocked('岗位筛选或截图核验已不满足要求。')
    research = d.get('research', {})
    if research.get('status') != 'verified_sources' or not research.get('claim_review', {}).get('identity_quotes_verified'):
        raise DeliveryBlocked('公司研究及身份引用未通过。')
    folder = (private / 'jobs' / job_id).resolve()
    exports = d.get('exports', {})
    expected = {'resume': 'resume-draft.docx', 'manifest': 'manifest.json', 'greeting': 'greeting-draft.txt'}
    paths = {}
    for key, name in expected.items():
        path = Path(exports.get(key, '')).resolve()
        if path != folder / name or not path.is_file():
            raise DeliveryBlocked('草稿文件路径或所属岗位不一致。')
        paths[key] = path
    manifest = json.loads(paths['manifest'].read_text(encoding='utf-8'))
    if (manifest['company'] != row['company'] or manifest['title'] != row['title']
            or row['company'] != d['job']['company']['value'] or row['title'] != d['job']['title']['value']
            or manifest['resume_sha256'] != digest(paths['resume'])):
        raise DeliveryBlocked('草稿岗位标记或文件校验值不一致。')
    facts = {f['id']: f for f in store.facts(confirmed_only=True)}
    ids = {f['id'] for f in manifest['facts']}
    if not ids or ids != set(d['export_fact_ids']):
        raise DeliveryBlocked('草稿素材清单不一致。')
    for fact in manifest['facts']:
        current = facts.get(fact['id'])
        if not current or any(fact[k] != current[k] for k in ('text', 'section', 'source', 'locator')):
            raise DeliveryBlocked('草稿引用素材已修改或撤销确认。')
    from job_agent.exporter import greeting_text
    if paths['greeting'].read_text(encoding='utf-8') != greeting_text(d['job']):
        raise DeliveryBlocked('招呼语已偏离经过审核的固定模板，请重新生成草稿。')
    return {'row': row, 'paths': paths, 'manifest': manifest, 'hash': manifest['resume_sha256']}


class Ledger:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS outbound (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                    target_url TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reserved_day TEXT,
                    evidence TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '', owner_pid INTEGER,
                    UNIQUE(job_id,kind)
                );
                CREATE TABLE IF NOT EXISTS outbound_events (
                    id INTEGER PRIMARY KEY, action_id TEXT NOT NULL, status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recruiter_replies (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, thread_url TEXT NOT NULL,
                    text TEXT NOT NULL, classification TEXT NOT NULL, captured_at TEXT NOT NULL,
                    evidence_path TEXT NOT NULL, evidence_sha256 TEXT NOT NULL DEFAULT ''
                );
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(outbound)')}
            if 'owner_pid' not in columns:
                db.execute('ALTER TABLE outbound ADD COLUMN owner_pid INTEGER')
            columns={r['name'] for r in db.execute('PRAGMA table_info(recruiter_replies)')}
            if 'evidence_sha256' not in columns:
                db.execute("ALTER TABLE recruiter_replies ADD COLUMN evidence_sha256 TEXT NOT NULL DEFAULT ''")

    def actions(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbound ORDER BY created_at DESC')]

    def action(self, action_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM outbound WHERE id=?', (action_id,)).fetchone()
        if row is None:
            raise DeliveryBlocked('执行计划不存在。')
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['evidence'] = json.loads(result['evidence'])
        return result

    def transition(self, action_id, status, reason='', evidence=None, allowed=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT status FROM outbound WHERE id=?', (action_id,)).fetchone()
            if not current or (allowed is not None and current['status'] not in allowed):
                raise DeliveryBlocked('执行状态发生变化，已停止。')
            db.execute('UPDATE outbound SET status=?,reason=?,evidence=?,updated_at=? WHERE id=?',
                       (status, reason, json.dumps(evidence or {}, ensure_ascii=False), utc_now(), action_id))
            db.execute('INSERT INTO outbound_events(action_id,status,note,created_at) VALUES(?,?,?,?)',
                       (action_id, status, reason, utc_now()))

    def recover(self):
        with self.store.connect() as db:
            # A crashed process may have clicked the final control; never assume failure.
            rows = db.execute("SELECT id,owner_pid FROM outbound WHERE status IN ('reserved','executing')").fetchall()
        recovered=0
        for row in rows:
            if not process_alive(row['owner_pid']):
                self.transition(row['id'], 'unknown', '上次执行中断，可能已生效；禁止自动重试。', allowed={'reserved','executing'})
                recovered+=1
        return recovered

    def counts(self, day=None):
        day = day or datetime.now().astimezone().date().isoformat()
        with self.store.connect() as db:
            active = "status IN ('reserved','executing','succeeded','unknown')"
            total = {r['kind']: r['n'] for r in db.execute("SELECT kind,count(*) n FROM outbound WHERE status='succeeded' GROUP BY kind")}
            touches = db.execute(f"SELECT count(DISTINCT job_id) FROM outbound o WHERE reserved_day=? AND kind IN ('boss_greeting','external_submit') AND {active} AND NOT (kind='external_submit' AND EXISTS (SELECT 1 FROM outbound b WHERE b.job_id=o.job_id AND b.kind='boss_greeting' AND b.status='succeeded'))", (day,)).fetchone()[0]
            resumes = db.execute(f"SELECT count(*) FROM outbound WHERE reserved_day=? AND kind IN ('resume_send','external_submit') AND {active}", (day,)).fetchone()[0]
            unknown = db.execute("SELECT count(*) FROM outbound WHERE status='unknown'").fetchone()[0]
        return {'today_reserved_jobs': touches, 'today_reserved_resumes': resumes,
                'boss_greeting': total.get('boss_greeting', 0), 'resume_send': total.get('resume_send', 0),
                'external_submit': total.get('external_submit', 0), 'unknown': unknown}

    def reserve(self, action_id, policy: Policy, day=None):
        policy.validate()
        day = day or datetime.now().astimezone().date().isoformat()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM outbound WHERE id=?', (action_id,)).fetchone()
            if not row or row['status'] not in {'planned', 'previewed', 'blocked'}:
                raise DeliveryBlocked('已处理、结果不明或执行中的计划不能重试。')
            # One browser writer across all worker processes.
            if db.execute("SELECT 1 FROM outbound WHERE status IN ('reserved','executing')").fetchone():
                raise BatchStopped('另一个投递操作正在执行。')
            # Both attachment channels share a cap; reaching it stops new greetings too.
            count = db.execute("SELECT count(*) FROM outbound WHERE reserved_day=? AND kind IN ('resume_send','external_submit') AND status IN ('reserved','executing','succeeded','unknown')", (day,)).fetchone()[0]
            if count >= policy.daily_resume_sends:
                raise BatchStopped('已达到今日简历投递额度（含网申和待核实预占），停止批次。')
            if row['kind'] in {'resume_send','external_submit'}:
                previous = db.execute("SELECT 1 FROM outbound WHERE job_id=? AND kind IN ('resume_send','external_submit') AND status IN ('succeeded','unknown')", (row['job_id'],)).fetchone()
                if previous:
                    raise DeliveryBlocked('该岗位已经投递过附件或结果不明，不跨渠道重复投递。')
            if row['kind'] in TOUCHES:
                previous = db.execute("SELECT kind,status FROM outbound WHERE job_id=? AND kind IN ('boss_greeting','external_submit') AND status IN ('succeeded','unknown')", (row['job_id'],)).fetchone()
                follow_up=False
                if previous and row['kind']=='external_submit' and previous['kind']=='boss_greeting' and previous['status']=='succeeded':
                    payload=json.loads(row['payload'])
                    reply=self.reply(payload.get('reply_id',''),row['job_id'],classification='external_link')
                    links={u.rstrip('。，、)）') for u in re.findall(r'https?://[^\s<>"\u3000]+',reply['text'])}
                    follow_up=row['target_url'] in links
                if previous and not follow_up:
                    raise DeliveryBlocked('该岗位已经触达或结果不明，不重复投递。')
                already_today=db.execute("SELECT 1 FROM outbound WHERE job_id=? AND reserved_day=? AND kind IN ('boss_greeting','external_submit') AND status IN ('succeeded','unknown')",(row['job_id'],day)).fetchone()
                count = db.execute("SELECT count(DISTINCT job_id) FROM outbound o WHERE reserved_day=? AND kind IN ('boss_greeting','external_submit') AND status IN ('reserved','executing','succeeded','unknown') AND NOT (kind='external_submit' AND EXISTS (SELECT 1 FROM outbound b WHERE b.job_id=o.job_id AND b.kind='boss_greeting' AND b.status='succeeded'))", (day,)).fetchone()[0]
                if not follow_up and not already_today and count >= policy.daily_new_jobs:
                    raise BatchStopped('已达到今日新岗位触达上限，停止批次。')
            db.execute("UPDATE outbound SET status='reserved',reserved_day=?,reason='',updated_at=?,owner_pid=? WHERE id=?", (day, utc_now(),os.getpid(), action_id))
            db.execute('INSERT INTO outbound_events(action_id,status,note,created_at) VALUES(?,?,?,?)', (action_id,'reserved','原子预占每日额度',utc_now()))

    def check_contact(self, job_id):
        with self.store.connect() as db:
            row=db.execute('SELECT text FROM recruiter_replies WHERE job_id=? ORDER BY captured_at DESC,rowid DESC LIMIT 1',(job_id,)).fetchone()
        if row and classify_reply(row['text']) in {'declined','sensitive'}:
            raise DeliveryBlocked('最新回复拒绝联系或涉及敏感要求，停止该岗位外发。')

    def add_reply(self, job_id, thread_url, text, evidence_path: Path):
        if not evidence_path.is_file():
            raise DeliveryBlocked('回复缺少本地采集证据。')
        source=json.loads(evidence_path.read_text(encoding='utf-8'))
        job=self.store.job(job_id)
        if source.get('job_url')!=job['url'] or source.get('page_url')!=thread_url or source.get('text')!=text:
            raise DeliveryBlocked('回复证据与岗位或会话不一致。')
        image=Path(source.get('screenshot','')).resolve()
        if image.parent!=evidence_path.parent.resolve() or not image.is_file() or digest(image)!=source.get('screenshot_sha256'):
            raise DeliveryBlocked('回复截图校验失败。')
        evidence_hash=digest(evidence_path)
        key = hashlib.sha256((job_id + thread_url + evidence_hash).encode()).hexdigest()
        classification = classify_reply(text)
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO recruiter_replies VALUES(?,?,?,?,?,?,?,?)',
                       (key,job_id,thread_url,text,classification,utc_now(),str(evidence_path.resolve()),evidence_hash))
        return key, classification

    def reply(self, reply_id, job_id, *, classification='resume_requested'):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM recruiter_replies WHERE id=? AND job_id=?', (reply_id,job_id)).fetchone()
            latest = db.execute('SELECT id FROM recruiter_replies WHERE job_id=? ORDER BY captured_at DESC,rowid DESC LIMIT 1',(job_id,)).fetchone()
        if not row or row['classification'] != classification or classify_reply(row['text'])!=classification:
            raise DeliveryBlocked('没有对应岗位的明确请求；普通回复不触发发送。')
        if not latest or latest['id']!=reply_id:
            raise DeliveryBlocked('该岗位已有更新的回复，请重新读取并生成计划。')
        source=Path(row['evidence_path'])
        if not source.is_file() or digest(source)!=row['evidence_sha256']:
            raise DeliveryBlocked('回复来源凭据已变化或缺失。')
        evidence=json.loads(source.read_text(encoding='utf-8'))
        image=Path(evidence.get('screenshot',''))
        if not image.is_file() or digest(image)!=evidence.get('screenshot_sha256'):
            raise DeliveryBlocked('回复截图已变化或缺失。')
        recent_reply_time(row['captured_at'])
        recent_reply_time(evidence.get('captured_at'))
        recent_reply_time(evidence.get('message_sent_at'))
        return {**dict(row),'message_sent_at':evidence['message_sent_at']}


def prepare(ledger: Ledger, private: Path, job_id: str, kind: str, *, target_url='', reply_id='', fields=None) -> dict:
    if kind not in KINDS:
        raise DeliveryBlocked('不支持的投递动作。')
    ledger.check_contact(job_id)
    bundle = validated_bundle(ledger.store, private, job_id)
    if fields:raise DeliveryBlocked('暂不接受未经审核的自由表单答案。')
    row = bundle['row']
    target = target_url or row['url']
    parts = urlsplit(target)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in {None,443}:
        raise DeliveryBlocked('目标必须是无账号密码的 HTTPS 地址。')
    if kind == 'boss_greeting':
        from job_agent.browser_capture import detail_url
        if target != detail_url(row['url']):
            raise DeliveryBlocked('开聊链接与岗位不一致。')
    elif kind == 'resume_send':
        if parts.hostname not in {'zhipin.com','www.zhipin.com'} or parts.path!='/web/geek/chat':
            raise DeliveryBlocked('简历发送目标必须是对应的 BOSS 会话。')
        reply = ledger.reply(reply_id, job_id)
        if target != reply['thread_url']:
            raise DeliveryBlocked('简历请求与目标会话不一致。')
    elif kind == 'external_submit':
        from job_agent.sources import public_url
        public_url(target)
        parts = urlsplit(target)
        if parts.scheme != 'https' or parts.hostname in {'zhipin.com','www.zhipin.com'}:
            raise DeliveryBlocked('正式网申目标必须为 HTTPS 外链。')
        # A link must occur verbatim in the target JD or a captured recruiter reply.
        texts = [row['jd']]
        with ledger.store.connect() as db:
            latest=db.execute('SELECT id FROM recruiter_replies WHERE job_id=? ORDER BY captured_at DESC,rowid DESC LIMIT 1',(job_id,)).fetchone()
        if latest:
            try:
                reply=ledger.reply(latest['id'],job_id,classification='external_link')
                reply_links={u.rstrip('。，、)）') for u in re.findall(r'https?://[^\s<>"\u3000]+',reply['text'])}
                if target in reply_links:
                    texts.append(reply['text']);reply_id=reply['id']
            except DeliveryBlocked:pass
        links = {u.rstrip('。，、)）') for t in texts for u in re.findall(r'https?://[^\s<>"\u3000]+', t)}
        if target not in links:
            raise DeliveryBlocked('网申外链不在该岗位原文或已采集的招聘回复中。')
    payload = {'job_url':row['url'], 'company':row['company'], 'title':row['title'],
               'resume_sha256':bundle['hash'], 'fact_ids':bundle['row']['detail']['export_fact_ids'],
               'greeting':bundle['paths']['greeting'].read_text(encoding='utf-8'), 'reply_id':reply_id,
               'reply_text':reply['text'] if kind=='resume_send' else '',
               'reply_sent_at':reply['message_sent_at'] if kind=='resume_send' else '',
               'source_revision':row['detail'].get('source_revision', ''), 'fields':{}}
    with ledger.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT id,status FROM outbound WHERE job_id=? AND kind=?',(job_id,kind)).fetchone()
        if old and old['status'] in {'reserved','executing','succeeded','unknown'}:
            return ledger.action(old['id'])
        action_id = old['id'] if old else uuid4().hex
        if old:
            db.execute("UPDATE outbound SET target_url=?,payload=?,status='planned',reason='',updated_at=? WHERE id=?",
                       (target,json.dumps(payload,ensure_ascii=False),utc_now(),action_id))
        else:
            db.execute('INSERT INTO outbound(id,job_id,kind,target_url,payload,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (action_id,job_id,kind,target,json.dumps(payload,ensure_ascii=False),'planned',utc_now(),utc_now()))
    return ledger.action(action_id)


class Executor:
    def __init__(self, ledger: Ledger, private: Path, policy: Policy, adapter, cancelled=lambda: False):
        self.ledger, self.private, self.policy, self.adapter, self.cancelled = ledger, private, policy, adapter, cancelled

    def run(self, action_id):
        action = self.ledger.action(action_id)
        if action['status'] in {'succeeded','unknown','reserved','executing'}:
            raise DeliveryBlocked('该计划不能自动重复执行。')
        self.policy.validate()
        def check():
            if self.cancelled():
                raise DeliveryBlocked('执行已取消。')
            self.ledger.check_contact(action['job_id'])
            bundle = validated_bundle(self.ledger.store, self.private, action['job_id'])
            payload=action['payload'];row=bundle['row']
            if (bundle['hash'] != payload['resume_sha256'] or payload['company']!=row['company']
                    or payload['title']!=row['title'] or payload['job_url']!=row['url']
                    or payload.get('source_revision','')!=row['detail'].get('source_revision','')
                    or set(payload['fact_ids'])!=set(row['detail']['export_fact_ids'])
                    or payload['greeting']!=bundle['paths']['greeting'].read_text(encoding='utf-8')):
                raise DeliveryBlocked('计划引用的草稿已过期或内容不符，请重新生成计划。')
            if action['kind']=='resume_send':
                reply=self.ledger.reply(action['payload']['reply_id'], action['job_id'])
                if (reply['text']!=action['payload'].get('reply_text') or reply['thread_url']!=action['target_url']
                        or reply['message_sent_at']!=action['payload'].get('reply_sent_at')):
                    raise DeliveryBlocked('计划与最新简历请求不一致。')
            if action['kind']=='external_submit':
                source=row['jd']
                if action['payload']['reply_id']:
                    reply=self.ledger.reply(action['payload']['reply_id'],action['job_id'],classification='external_link')
                    source=reply['text']
                links={u.rstrip('。，、)）') for u in re.findall(r'https?://[^\s<>"\u3000]+',source)}
                if action['target_url'] not in links:
                    raise DeliveryBlocked('网申计划与来源中的完整链接不一致。')
            return bundle
        try:
            bundle = check()
            plan = self.adapter.inspect(action)
            if self.cancelled():
                raise DeliveryBlocked('执行已取消。')
            # Inspection is read-only, even when production mode is enabled.
            if self.policy.mode == 'dry_run':
                self.ledger.transition(action_id,'previewed','仅完成只读预检；未填写、上传或发送。',plan,allowed={'planned','previewed','blocked'})
                return 'previewed'
            if self.adapter.adapter_id not in self.policy.enabled_adapters:
                raise DeliveryBlocked('该站点适配器尚未启用真实执行。')
            self.ledger.reserve(action_id,self.policy)
        except BatchStopped as exc:
            self.ledger.transition(action_id,'blocked',str(exc),allowed={'planned','previewed','blocked'})
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, DeliveryBlocked) else '预检失败，未执行外发。'
            self.ledger.transition(action_id,'blocked',message,allowed={'planned','previewed','blocked'})
            return 'blocked'
        try:
            bundle = check()
            # A stable per-job/per-hash attachment name prevents the global default resume being used.
            attachment = self.private/'outbound'/action_id/f'resume-{action["job_id"][:12]}-{bundle["hash"][:16]}.docx'
            attachment.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(bundle['paths']['resume'],attachment)
            if digest(attachment) != bundle['hash']:
                raise DeliveryBlocked('待发送附件校验失败。')
            self.ledger.transition(action_id,'executing','即将填写或外发；异常后需核实。',allowed={'reserved'})
            evidence = self.adapter.execute(action,attachment,check)
            evidence['executed_at']=utc_now()
            evidence['arrival_if_applied_today']=(datetime.now().astimezone().date()+timedelta(days=14)).isoformat()
            if not self.adapter.verify(action,evidence,attachment):
                self.ledger.transition(action_id,'unknown','没有足够成功证据，禁止自动重试。',evidence,allowed={'executing'})
                return 'unknown'
            self.ledger.transition(action_id,'succeeded','已读取匹配目标和本次动作的成功凭据。',evidence,allowed={'executing'})
            return 'succeeded'
        except Exception as exc:
            reason = str(exc) if isinstance(exc,DeliveryBlocked) else '外发过程异常，结果需要核实。'
            self.ledger.transition(action_id,'unknown',reason,allowed={'reserved','executing'})
            if isinstance(exc,BatchStopped):
                raise
            return 'unknown'
