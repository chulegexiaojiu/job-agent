"""Isolated UIA worker: --live requires local adapter policy as a second gate."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from dataclasses import replace
import json
import re
from pathlib import Path

from job_agent.browser_delivery import BossAdapter, FormAdapter, PlannedBrowser
from job_agent.delivery import (DeliveryBlocked, BatchStopped, Executor, Ledger, Policy, prepare)
from job_agent.materials import read_identity
from job_agent.settings import PRIVATE, atomic_write
from job_agent.store import Store


def load_policy(private: Path) -> Policy:
    path=private/'delivery-policy.json'
    if not path.exists():return Policy()
    if path.stat().st_size>65536:raise DeliveryBlocked('执行配置过大。')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or set(data)-{'mode','daily_new_jobs','daily_resume_sends','enabled_adapters'}:
        raise DeliveryBlocked('执行配置包含未知字段。')
    if not isinstance(data.get('enabled_adapters',[]),list):raise DeliveryBlocked('适配器配置必须为列表。')
    data['enabled_adapters']=tuple(data.get('enabled_adapters',()))
    if not all(isinstance(v,str) for v in data['enabled_adapters']):raise DeliveryBlocked('适配器配置无效。')
    result=Policy(**data);result.validate();return result


def adapter_for(action, private, store):
    if action['kind']!='external_submit':return BossAdapter(private)
    directory=private/'form-profiles'
    profiles=[]
    for path in directory.glob('*.json'):
        if path.stat().st_size>65536:continue
        profile=json.loads(path.read_text(encoding='utf-8'))
        if profile.get('url')==action['target_url']:profiles.append(profile)
    if len(profiles)!=1:raise DeliveryBlocked('该网申链接尚无唯一经过验证的表单配置。')
    from job_agent.form_profile import validate_profile
    profile=validate_profile(profiles[0])
    # Domain identity evidence must be among this job's actually fetched research pages.
    from job_agent.sources import quote_present
    research=store.job(action['job_id'])['detail']['research']
    pages={p['url']:p['text'] for p in research.get('pages',[])}
    quote=profile.get('identity_evidence_quote','')
    source=profile.get('identity_source_url','')
    links={u.rstrip('。，、)）') for u in re.findall(r'https?://[^\s<>"\u3000]+',pages.get(source,''))}
    if (source not in pages or len(quote)<8 or not quote_present(quote,pages[source])
            or action['target_url'] not in links):
        raise DeliveryBlocked('公司研究来源尚未支持这个确切网申链接。')
    return FormAdapter(private,profile,read_identity(private))


@contextmanager
def browser_session_lock(private):
    """Share the capture worker lock and preserve the delivery stop classification."""
    from job_agent.browser_lock import BrowserSessionBusy, browser_session_lock as shared_lock
    try:
        with shared_lock(private):yield
    except BrowserSessionBusy as exc:
        raise BatchStopped(str(exc)) from None


def run_actions(private: Path, ids: list[str], *, live=False, cancelled=lambda:False, progress=lambda result:None):
    with browser_session_lock(private):
        return _run_actions(private,ids,live=live,cancelled=cancelled,progress=progress)


def _run_actions(private: Path, ids: list[str], *, live=False, cancelled=lambda:False, progress=lambda result:None):
    store=Store(private/'workspace.sqlite3');ledger=Ledger(store)
    policy=load_policy(private)
    if live and policy.mode!='live':raise DeliveryBlocked('本机策略仍是不发送验证，未启用真实执行。')
    if not live:policy=replace(policy,mode='dry_run')
    results=[]
    browser=PlannedBrowser(cancelled) if live else None
    for action_id in ids:
        if cancelled():break
        action=ledger.action(action_id)
        try:
            adapter=adapter_for(action,private,store)
            if live:
                if adapter.adapter_id not in policy.enabled_adapters:
                    raise DeliveryBlocked('该站点适配器尚未启用真实执行。')
                if isinstance(adapter,(BossAdapter,FormAdapter)):adapter.browser_factory=browser
            status=Executor(ledger,private,policy,adapter,cancelled).run(action_id)
            results.append({'id':action_id,'status':status})
        except BatchStopped as exc:
            results.append({'id':action_id,'status':'batch_stopped','reason':str(exc)})
            progress(results)
            break
        except Exception as exc:
            message=str(exc) if isinstance(exc,DeliveryBlocked) else '执行配置或页面预检异常。'
            if action['status'] in {'planned','previewed','blocked'}:
                ledger.transition(action_id,'blocked',message,allowed={'planned','previewed','blocked'})
            results.append({'id':action_id,'status':'blocked','reason':message})
        progress(results)
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--private',type=Path,default=PRIVATE)
    p.add_argument('--request',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--cancel-file',type=Path,required=True)
    p.add_argument('--live',action='store_true')
    args=p.parse_args()
    result={'status':'running','mode':'live' if args.live else 'dry_run','results':[]}
    def save(rows):
        result['results']=rows
        atomic_write(args.report,json.dumps(result,ensure_ascii=False,indent=2).encode())
    save([])
    try:
        request=json.loads(args.request.read_text(encoding='utf-8'))
        if request.get('operation')=='capture_reply':
            store=Store(args.private/'workspace.sqlite3');ledger=Ledger(store)
            job=store.job(request['job_id'])
            action={'job_id':job['id'],'target_url':request['thread_url'],
                    'payload':{'job_url':job['url'],'company':job['company'],'title':job['title']}}
            adapter=BossAdapter(args.private)
            if args.cancel_file.exists():raise DeliveryBlocked('读取回复已取消。')
            with browser_session_lock(args.private):
                text,path=adapter.capture_reply(action)
                reply_id,classification=ledger.add_reply(job['id'],request['thread_url'],text,path)
                result.update(reply_id=reply_id,classification=classification)
                if classification=='resume_requested' and not args.cancel_file.exists():
                    plan=prepare(ledger,args.private,job['id'],'resume_send',target_url=request['thread_url'],reply_id=reply_id)
                    result['created_plan']=plan['id']
        else:
            ids=request['ids']
            if not isinstance(ids,list) or not 1<=len(ids)<=120 or any(not isinstance(i,str) for i in ids):
                raise DeliveryBlocked('执行计划数量或格式无效。')
            result['results']=run_actions(args.private,ids,live=args.live,cancelled=args.cancel_file.exists,progress=save)
        stopped=next((r for r in result['results'] if r['status']=='batch_stopped'),None)
        result['status']='cancelled' if args.cancel_file.exists() else 'stopped' if stopped else 'completed'
        if stopped:result['reason']=stopped.get('reason','批次已停止。')
    except Exception as exc:
        result.update(status='stopped',reason=str(exc) if isinstance(exc,DeliveryBlocked) else '执行工作进程异常；请查看本地记录。')
    save(result['results'])
    print(json.dumps({'ok':True,'status':result['status']},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
