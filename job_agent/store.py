"""Private SQLite state. Review work never records an application as sent."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_agent.settings import PRIVATE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_job_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('请填写完整的 http(s) 岗位链接，不要包含账号密码。')
    if parts.port not in {None, 80, 443}:
        raise ValueError('岗位链接端口不受支持。')
    ignored = {'spm', 'from', 'source', 'sessionid', 'lid', 'securityid', 'ka', 'seid', 'ref', 'referrer'}
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in ignored and not k.lower().startswith('utm_')]
    return urlunsplit((parts.scheme.lower(), parts.hostname.lower(), parts.path.rstrip('/') or '/', urlencode(sorted(query)), ''))


class Store:
    def __init__(self, path: Path = PRIVATE / 'workspace.sqlite3'):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, locator TEXT NOT NULL,
                section TEXT NOT NULL, text TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, jd TEXT NOT NULL,
                screenshot TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '', quality REAL, match REAL,
                detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL
              );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_facts(self, facts: list[dict]) -> int:
        with self.connect() as db:
            before = db.total_changes
            for fact in facts:
                db.execute('INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?,?)',
                           (fact['id'], fact['source'], fact['locator'], fact['section'], fact['text'], 0, utc_now()))
            return db.total_changes - before

    def facts(self, confirmed_only: bool = False) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM facts' + (' WHERE confirmed=1' if confirmed_only else '') +
                              ' ORDER BY created_at, rowid').fetchall()
            return [dict(row) for row in rows]

    def confirm_facts(self, ids: list[str], value: bool) -> None:
        with self.connect() as db:
            db.executemany('UPDATE facts SET confirmed=? WHERE id=?', [(int(value), fid) for fid in ids])
            if not value:
                revoked = set(ids)
                for row in db.execute("SELECT id,detail FROM jobs WHERE status='draft_ready'").fetchall():
                    detail = json.loads(row['detail'])
                    used = set(detail.get('export_fact_ids', detail.get('assessment', {}).get('selected_fact_ids', [])))
                    if used & revoked:
                        detail['stale_exports'] = detail.pop('exports', {})
                        detail['pause_reasons'] = ['部分引用素材已撤销确认，旧草稿已过期；请重新分析生成。']
                        db.execute("UPDATE jobs SET status='facts_required',quality=NULL,match=NULL,detail=?,updated_at=? WHERE id=?",
                                   (json.dumps(detail, ensure_ascii=False), utc_now(), row['id']))

    def add_job(self, url: str, jd: str, screenshot: str = '') -> tuple[str, bool]:
        url = canonical_job_url(url)
        if not jd.strip() or len(jd) > 40000:
            raise ValueError('岗位文字应为 1～40000 字符。')
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as db:
            existing = db.execute('SELECT id FROM jobs WHERE url=?', (url,)).fetchone()
            if existing:
                return existing['id'], False
            db.execute('INSERT INTO jobs(id,url,jd,screenshot,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (job_id, url, jd.strip(), screenshot, 'queued', now, now))
            return job_id, True

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()]

    def job(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('岗位不存在。')
        result = dict(row)
        result['detail'] = json.loads(result['detail'])
        return result

    def claim(self, job_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status IN ('queued','paused','error','cancelled','facts_required','draft_ready')",
                                (utc_now(), job_id))
            return cursor.rowcount == 1

    def update(self, job_id: str, **fields) -> None:
        allowed = {'status', 'title', 'company', 'quality', 'match', 'detail', 'screenshot'}
        if not fields or fields.keys() - allowed:
            raise ValueError('Invalid job state update.')
        if 'detail' in fields:
            fields['detail'] = json.dumps(fields['detail'], ensure_ascii=False)
        fields['updated_at'] = utc_now()
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(name + '=?' for name in fields) + ' WHERE id=?',
                       [*fields.values(), job_id])

    def event(self, job_id: str, kind: str, detail: str) -> None:
        with self.connect() as db:
            db.execute('INSERT INTO events(job_id,kind,detail,created_at) VALUES(?,?,?,?)',
                       (job_id, kind, detail, utc_now()))

    def events(self, job_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE job_id=? ORDER BY id', (job_id,))]

    def recover_interrupted(self) -> int:
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET status='paused',updated_at=? WHERE status='running'", (utc_now(),))
            return cursor.rowcount
