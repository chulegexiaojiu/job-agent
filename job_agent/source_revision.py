"""Explicit local source revisions; never silently replace a duplicate import."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from job_agent.browser_lock import browser_session_lock
from job_agent.client import image_data
from job_agent.settings import atomic_write
from job_agent.store import Store, canonical_job_url, utc_now


def replace_source(store: Store, private: Path, job_id: str, url: str, text: str, screenshot: Path) -> str:
    """Save a revision, then atomically invalidate review and unexecuted plans."""
    if not text.strip() or len(text) > 40000:
        raise ValueError('岗位文字应为 1～40000 字符。')
    image_data(screenshot)
    url = canonical_job_url(url)
    with browser_session_lock(private), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or row['url'] != url:
            raise ValueError('更新链接必须与所选岗位的完整链接一致；新岗位请加入待办。')
        if row['status'] == 'running':
            raise ValueError('岗位正在分析，请等待结束后更新来源。')
        has_ledger = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbound'").fetchone()
        if has_ledger and db.execute("SELECT 1 FROM outbound WHERE job_id=? AND status IN ('reserved','executing')", (job_id,)).fetchone():
            raise ValueError('岗位正在执行投递，不能更新来源。')
        folder = private / 'jobs' / job_id / 'source-revisions' / uuid4().hex
        folder.mkdir(parents=True)
        target = folder / ('source' + screenshot.suffix.lower())
        shutil.copyfile(screenshot, target)
        image_data(target)  # Validate the saved copy, not only the caller's mutable file.
        old = dict(row)
        old['detail'] = json.loads(old['detail'])
        # Archive old files before a later analysis can overwrite standard export paths.
        exports = old['detail'].get('exports') or old['detail'].get('stale_exports') or {}
        job_folder = (private / 'jobs' / job_id).resolve()
        archived = {}
        for key, value in exports.items():
            if not isinstance(value, str):
                continue
            source = Path(value).resolve()
            if source.is_file() and source.is_relative_to(job_folder):
                dest = folder / 'previous-exports' / source.name
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, dest)
                archived[key] = str(dest.resolve())
        old_image = Path(old['screenshot']) if old['screenshot'] else None
        if old_image and old_image.is_file():
            shutil.copyfile(old_image, folder / ('previous-source' + old_image.suffix.lower()))
        capture = job_folder / 'source-capture.json'
        if capture.is_file():
            shutil.copyfile(capture, folder / 'previous-capture.json')
        atomic_write(folder / 'previous.json', json.dumps(old, ensure_ascii=False, indent=2).encode())
        now = utc_now()
        atomic_write(folder / 'revision.json', json.dumps({
            'job_id': job_id, 'url': url, 'text': text.strip(), 'screenshot': str(target.resolve()),
            'screenshot_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'text_sha256': hashlib.sha256(text.strip().encode('utf-8')).hexdigest(),
            'created_at': now, 'method': 'explicit_manual_update', 'sent': False
        }, ensure_ascii=False, indent=2).encode())
        detail = {'mode': 'dry_run', 'sent': False, 'source_revision': str(folder.resolve()),
                  'pause_reasons': ['来源已明确更新，旧研究、评分和草稿失效；请重新核验。']}
        if archived:
            detail['stale_exports'] = archived
        db.execute("UPDATE jobs SET jd=?,screenshot=?,status='queued',title='',company='',quality=NULL,match=NULL,detail=?,updated_at=? WHERE id=?",
                   (text.strip(), str(target.resolve()), json.dumps(detail, ensure_ascii=False), now, job_id))
        if has_ledger:
            ids = db.execute("SELECT id FROM outbound WHERE job_id=? AND status IN ('planned','previewed','blocked')", (job_id,)).fetchall()
            for action in ids:
                reason = '岗位来源已更新，计划失效；请重新分析并建立计划。'
                db.execute("UPDATE outbound SET status='blocked',reason=?,updated_at=? WHERE id=?", (reason, now, action['id']))
                db.execute('INSERT INTO outbound_events(action_id,status,note,created_at) VALUES(?,?,?,?)', (action['id'], 'blocked', reason, now))
        db.execute('INSERT INTO events(job_id,kind,detail,created_at) VALUES(?,?,?,?)',
                   (job_id, 'source_revision', '明确更新来源；已归档旧来源及草稿，未分析或投递。', now))
    return str(folder.resolve())
