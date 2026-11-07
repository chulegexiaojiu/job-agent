"""Synthetic outbound tests. No recruiter or external application is contacted."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_agent.delivery import (BatchStopped,DeliveryBlocked,Executor,Ledger,Policy,classify_reply,prepare,validated_bundle)
from job_agent.exporter import export_draft
from job_agent.models import crosscheck
from job_agent.store import Store, utc_now
from job_agent.browser_delivery import FormAdapter, BossAdapter
from job_agent.delivery_worker import load_policy,run_actions
from job_agent.form_profile import validate_profile
from test_workbench import job_fixture,research_fixture


def build_job(store,root,index=1):
    job=job_fixture();job.title.value+=' '+str(index);job.title.quote=job.title.value
    fact={'id':'f1','source':'synthetic.txt','locator':'line 1','section':'项目经历','text':'本地 RAG 测试项目，未企业部署。'}
    store.add_facts([fact]);store.confirm_facts(['f1'],True)
    url=f'https://www.zhipin.com/job_detail/test{index}.html'
    external=f'https://careers.example.test/apply/{index}?token=example'
    job_id,_=store.add_job(url,'synthetic JD '+external)
    exports=export_draft(root/'jobs'/job_id,job.model_dump(),store.facts(True),['f1'],{'name':'合成测试者','email':'test@example.test'})
    research=research_fixture();research['claim_review']={'identity_quotes_verified':True}
    detail={'job':job.model_dump(),'crosscheck':crosscheck(job,job),'research':research,'exports':exports,
            'export_fact_ids':['f1'],'pause_reasons':[],'sent':False}
    store.update(job_id,status='draft_ready',title=job.title.value,company=job.company.value,detail=detail)
    return job_id,external


class Adapter:
    adapter_id='synthetic'
    def __init__(self,fail=False,verified=True):self.calls=[];self.fail=fail;self.verified=verified
    def inspect(self,a):self.calls.append('inspect');return {'target_matches':True}
    def execute(self,a,path,checkpoint):
        checkpoint();self.calls.append('execute')
        if self.fail:raise RuntimeError('uncertain')
        self.path=path
        return {'receipt_id':'TEST-1234'}
    def verify(self,a,e,path):self.calls.append('verify');return self.verified


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'test.sqlite3');self.ledger=Ledger(self.store)
        self.job,self.external=build_job(self.store,self.root)
        self.live=Policy(mode='live',enabled_adapters=('synthetic',))
    def plan(self,job=None,kind='boss_greeting',**kw):return prepare(self.ledger,self.root,job or self.job,kind,**kw)
    def execute(self,plan,adapter=None,policy=None):
        adapter=adapter or Adapter();return Executor(self.ledger,self.root,policy or self.live,adapter).run(plan['id'])
    def reply(self,text,job=None):
        from uuid import uuid4
        job=job or self.job
        folder=self.root/'replies'/uuid4().hex;folder.mkdir(parents=True)
        file=folder/'receipt.json';image=folder/'result.png';image.write_bytes(b'synthetic')
        target='https://www.zhipin.com/web/geek/chat?bossId=synthetic'
        file.write_text(json.dumps({'job_url':self.store.job(job)['url'],'page_url':target,'text':text,
            'captured_at':utc_now(),'message_sent_at':utc_now(),
            'screenshot':str(image),'screenshot_sha256':hashlib.sha256(image.read_bytes()).hexdigest()}),encoding='utf-8')
        key,kind=self.ledger.add_reply(job,target,text,file)
        return key,target,file

    def test_default_never_executes_and_counts_remain_zero(self):
        p=self.plan();a=Adapter();self.assertEqual(self.execute(p,a,Policy()),'previewed')
        self.assertEqual(a.calls,['inspect']);self.assertEqual(self.ledger.counts()['boss_greeting'],0)
        self.assertEqual(self.ledger.counts()['today_reserved_jobs'],0)

    def test_policy_blocks_unenabled_live_adapter(self):
        p=self.plan();a=Adapter();self.assertEqual(self.execute(p,a,Policy(mode='live')),'blocked')
        self.assertEqual(a.calls,['inspect']);self.assertEqual(self.ledger.counts()['today_reserved_jobs'],0)
        for bad in (True,0,121):
            with self.assertRaises(DeliveryBlocked):Policy(daily_new_jobs=bad).validate()

    def test_success_is_deduplicated_and_attachment_bound(self):
        p=self.plan();a=Adapter();self.assertEqual(self.execute(p,a),'succeeded')
        self.assertIn(self.job[:12],a.path.name)
        self.assertEqual(hashlib.sha256(a.path.read_bytes()).hexdigest(),p['payload']['resume_sha256'])
        self.assertEqual(self.plan()['id'],p['id'])
        with self.assertRaises(DeliveryBlocked):self.execute(p)
        self.assertEqual(self.ledger.counts()['boss_greeting'],1)

    def test_exception_or_unverified_success_becomes_unknown_and_never_retries(self):
        for fail,verified in [(True,True),(False,False)]:
            j,_=build_job(self.store,self.root,2+int(fail));p=self.plan(j)
            self.assertEqual(self.execute(p,Adapter(fail,verified)),'unknown')
            with self.assertRaises(DeliveryBlocked):self.execute(p)
        self.assertEqual(self.ledger.counts()['unknown'],2)
        self.assertEqual(self.ledger.counts()['today_reserved_jobs'],2)
        self.assertEqual(self.ledger.counts()['boss_greeting'],0)

    def test_atomic_quota_and_cross_channel_job_dedup(self):
        p=self.plan();self.assertEqual(self.execute(p),'succeeded')
        ext=self.plan(kind='external_submit',target_url=self.external)
        with self.assertRaises(DeliveryBlocked):self.ledger.reserve(ext['id'],self.live)
        other,_=build_job(self.store,self.root,2);p2=self.plan(other)
        with self.assertRaises(BatchStopped):self.ledger.reserve(p2['id'],Policy(daily_new_jobs=1))
        self.ledger.reserve(p2['id'],self.live,day='2099-01-01')
        self.assertEqual(self.ledger.counts('2099-01-01')['today_reserved_jobs'],1)

    def test_two_concurrent_reservations_only_one_writer(self):
        p1=self.plan();j,_=build_job(self.store,self.root,2);p2=self.plan(j)
        def reserve(p):
            try:self.ledger.reserve(p['id'],self.live);return True
            except DeliveryBlocked:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[p1,p2]))
        self.assertEqual(sum(results),1)

    def test_recovery_never_marks_live_worker_interrupted(self):
        p=self.plan();self.ledger.reserve(p['id'],self.live)
        self.assertEqual(self.ledger.recover(),0)
        with patch('job_agent.delivery.process_alive',return_value=False):self.assertEqual(self.ledger.recover(),1)
        self.assertEqual(self.ledger.action(p['id'])['status'],'unknown')
        with self.assertRaises(DeliveryBlocked):self.ledger.reserve(p['id'],self.live)

    def test_modified_or_revoked_draft_never_executes(self):
        p=self.plan();self.store.confirm_facts(['f1'],False);a=Adapter()
        self.assertEqual(self.execute(p,a),'blocked');self.assertFalse(a.calls)
        self.store.confirm_facts(['f1'],True)
        j,_=build_job(self.store,self.root,2);p=self.plan(j)
        resume=self.root/'jobs'/j/'resume-draft.docx';resume.write_bytes(resume.read_bytes()+b'changed')
        a=Adapter();self.assertEqual(self.execute(p,a),'blocked');self.assertFalse(a.calls)

    def test_modified_greeting_and_wrong_attachment_path_are_rejected(self):
        p=self.plan();row=self.store.job(self.job)
        Path(row['detail']['exports']['greeting']).write_text('invented accomplishments',encoding='utf-8')
        self.assertEqual(self.execute(p),'blocked')
        j,_=build_job(self.store,self.root,2);r=self.store.job(j);r['detail']['exports']['resume']=row['detail']['exports']['resume']
        self.store.update(j,detail=r['detail'])
        with self.assertRaises(DeliveryBlocked):self.plan(j)

    def test_cancel_before_execute_never_writes(self):
        p=self.plan();a=Adapter()
        status=Executor(self.ledger,self.root,self.live,a,lambda:True).run(p['id'])
        self.assertEqual(status,'blocked');self.assertFalse(a.calls)

    def test_cancel_after_reservation_does_not_retry(self):
        p=self.plan();a=Adapter();calls=[]
        def cancelled():calls.append(1);return len(calls)>=3
        status=Executor(self.ledger,self.root,self.live,a,cancelled).run(p['id'])
        self.assertEqual(status,'unknown');self.assertNotIn('execute',a.calls)

    def test_resume_needs_matching_explicit_reply_and_separate_quota(self):
        key,target,file=self.reply('你好')
        with self.assertRaises(DeliveryBlocked):self.plan(kind='resume_send',target_url=target,reply_id=key)
        key,target,file=self.reply('请发一份简历给我')
        p=self.plan(kind='resume_send',target_url=target,reply_id=key)
        self.assertEqual(self.execute(p),'succeeded')
        counts=self.ledger.counts();self.assertEqual(counts['resume_send'],1);self.assertEqual(counts['today_reserved_jobs'],0)
        self.assertEqual(counts['today_reserved_resumes'],1)
        with self.assertRaises(DeliveryBlocked):self.plan(kind='resume_send',target_url=target+'&other=1',reply_id=key)

    def test_external_url_requires_exact_source_link(self):
        with self.assertRaises(DeliveryBlocked):self.plan(kind='external_submit',target_url='https://other.example.test/apply')
        with self.assertRaises(DeliveryBlocked):self.plan(kind='external_submit',target_url=self.external+'&field=other')
        p=self.plan(kind='external_submit',target_url=self.external);self.assertEqual(p['target_url'],self.external)

    def test_reply_evidence_tamper_and_newer_decline_invalidate_plan(self):
        key,target,file=self.reply('请发一份简历给我')
        plan=self.plan(kind='resume_send',target_url=target,reply_id=key)
        image=file.with_name('result.png');image.write_bytes(b'changed')
        self.assertEqual(self.execute(plan),'blocked')
        key,target,file=self.reply('请发一份简历给我')
        plan=self.plan(kind='resume_send',target_url=target,reply_id=key)
        self.reply('不用发简历了')
        self.assertEqual(self.execute(plan),'blocked')
        self.assertEqual(self.ledger.counts()['today_reserved_resumes'],0)

    def test_plan_payload_tamper_is_blocked_before_adapter(self):
        plan=self.plan();payload=plan['payload'];payload['greeting']='unapproved text'
        with self.store.connect() as db:db.execute('UPDATE outbound SET payload=? WHERE id=?',(json.dumps(payload),plan['id']))
        adapter=Adapter();self.assertEqual(self.execute(plan,adapter),'blocked');self.assertEqual(adapter.calls,[])

    def test_form_counts_toward_shared_attachment_cap_and_stops_greetings(self):
        plan=self.plan(kind='external_submit',target_url=self.external)
        self.assertEqual(self.execute(plan),'succeeded')
        self.assertEqual(self.ledger.counts()['today_reserved_resumes'],1)
        other,_=build_job(self.store,self.root,2)
        greeting=self.plan(other)
        with self.assertRaises(BatchStopped):self.ledger.reserve(greeting['id'],Policy(daily_resume_sends=1))
        key,target,file=self.reply('请发一份简历给我')
        resume=self.plan(kind='resume_send',target_url=target,reply_id=key)
        with self.assertRaises(DeliveryBlocked):self.ledger.reserve(resume['id'],self.live)

    def test_explicit_recruiter_link_allows_followup_without_double_touch(self):
        greeting=self.plan();self.assertEqual(self.execute(greeting),'succeeded')
        key,target,file=self.reply('请到 '+self.external+' 提交简历')
        plan=self.plan(kind='external_submit',target_url=self.external)
        self.assertEqual(plan['payload']['reply_id'],key)
        self.assertEqual(self.execute(plan,policy=Policy(mode='live',daily_new_jobs=1,enabled_adapters=('synthetic',))),'succeeded')
        self.assertEqual(self.ledger.counts()['today_reserved_jobs'],1)
        self.assertEqual(self.ledger.counts()['today_reserved_resumes'],1)

    def test_followup_next_day_does_not_consume_new_job_quota(self):
        greeting=self.plan();self.assertEqual(self.execute(greeting),'succeeded')
        with self.store.connect() as db:db.execute("UPDATE outbound SET reserved_day='2000-01-01' WHERE id=?",(greeting['id'],))
        self.reply('请到 '+self.external+' 提交简历')
        form=self.plan(kind='external_submit',target_url=self.external)
        other,_=build_job(self.store,self.root,2);self.assertEqual(self.execute(self.plan(other)),'succeeded')
        policy=Policy(mode='live',daily_new_jobs=1,enabled_adapters=('synthetic',))
        self.assertEqual(self.execute(form,policy=policy),'succeeded')
        self.assertEqual(self.ledger.counts()['today_reserved_jobs'],1)
        self.assertEqual(self.ledger.counts()['today_reserved_resumes'],1)

    def test_unknown_attachment_blocks_other_channel_and_cap_stops_greeting(self):
        key,target,_=self.reply('请发一份简历给我')
        resume=self.plan(kind='resume_send',target_url=target,reply_id=key)
        self.assertEqual(self.execute(resume,Adapter(fail=True)),'unknown')
        form=self.plan(kind='external_submit',target_url=self.external)
        with self.assertRaises(DeliveryBlocked):self.ledger.reserve(form['id'],self.live)
        other,_=build_job(self.store,self.root,2)
        with self.assertRaises(BatchStopped):self.ledger.reserve(self.plan(other)['id'],Policy(daily_resume_sends=1))
        self.assertEqual(self.ledger.counts()['resume_send'],0)
        self.assertEqual(self.ledger.counts()['today_reserved_resumes'],1)

    def test_reply_wrong_job_thread_and_invalid_time_are_rejected(self):
        from datetime import datetime,timedelta,timezone
        key,target,file=self.reply('请发一份简历给我')
        other,_=build_job(self.store,self.root,2)
        with self.assertRaises(DeliveryBlocked):self.ledger.reply(key,other)
        with self.assertRaises(DeliveryBlocked):self.ledger.add_reply(self.job,target+'&wrong=1','请发一份简历给我',file)
        for offset in (-8,1):
            value=(datetime.now(timezone.utc)+timedelta(days=offset)).isoformat()
            with self.store.connect() as db:db.execute('UPDATE recruiter_replies SET captured_at=? WHERE id=?',(value,key))
            with self.assertRaises(DeliveryBlocked):self.ledger.reply(key,self.job)

    def test_recent_capture_cannot_refresh_old_missing_or_future_message(self):
        from datetime import datetime,timedelta,timezone
        for value in ('',None,'yesterday','2026-11-01T12:00:00',
                      (datetime.now(timezone.utc)-timedelta(days=8)).isoformat(),
                      (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()):
            with self.subTest(value=value):
                key,target,file=self.reply('请发一份简历给我')
                data=json.loads(file.read_text(encoding='utf-8'));data['message_sent_at']=value
                file.write_text(json.dumps(data),encoding='utf-8')
                key,_=self.ledger.add_reply(self.job,target,data['text'],file)
                with self.assertRaises(DeliveryBlocked):self.plan(kind='resume_send',target_url=target,reply_id=key)
        self.assertEqual(self.ledger.counts()['today_reserved_resumes'],0)

    def test_decline_blocks_existing_plan_and_new_external_plan(self):
        greeting=self.plan();self.reply('不要再联系')
        self.assertEqual(self.execute(greeting),'blocked')
        with self.assertRaises(DeliveryBlocked):self.plan(kind='external_submit',target_url=self.external)
        self.assertEqual(self.ledger.counts()['today_reserved_jobs'],0)

    def test_changed_external_link_is_rechecked_exactly(self):
        plan=self.plan(kind='external_submit',target_url=self.external)
        with self.store.connect() as db:db.execute('UPDATE outbound SET target_url=? WHERE id=?',(self.external+'&other=1',plan['id']))
        self.assertEqual(self.execute(plan),'blocked')

    def test_reply_classifier_never_treats_generic_or_sensitive_as_resume_consent(self):
        cases={'你好':'needs_review','请发简历':'resume_requested','不用发简历了':'declined','请把身份证和简历发来':'sensitive',
               '请到 https://company.test/apply 投简历':'external_link','明天可以面试吗':'interview','不要再联系':'declined','先不要发简历':'declined','如果合适再发简历':'needs_review'}
        cases.update({'如果合适再到 https://company.test/apply 提交简历':'needs_review',
                      '公司官网 https://company.test/':'needs_review',
                      '不要再联系，https://company.test/apply':'declined'})
        for value,result in cases.items():self.assertEqual(classify_reply(value),result)

    def test_policy_missing_defaults_and_invalid_config_fail_closed(self):
        self.assertEqual(load_policy(self.root).mode,'dry_run')
        (self.root/'delivery-policy.json').write_text('{"mode":"live","daily_new_jobs":999}',encoding='utf-8')
        with self.assertRaises(DeliveryBlocked):load_policy(self.root)
        for data in ([],{'enabled_adapters':'boss-uia-v1'},{'enabled_adapters':[1]}):
            (self.root/'delivery-policy.json').write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaises(DeliveryBlocked):load_policy(self.root)

    def test_browser_worker_session_lock_releases_after_error(self):
        from job_agent.delivery_worker import browser_session_lock
        with browser_session_lock(self.root):
            with self.assertRaises(BatchStopped):
                with browser_session_lock(self.root):pass
        with self.assertRaisesRegex(RuntimeError,'synthetic'):
            with browser_session_lock(self.root):raise RuntimeError('synthetic')
        with browser_session_lock(self.root):pass

    def test_batch_stops_on_platform_limit(self):
        p=self.plan();j,_=build_job(self.store,self.root,2);p2=self.plan(j)
        adapter=Adapter();adapter.inspect=Mock(side_effect=BatchStopped('平台限制'))
        # run_actions uses the same database path as the product.
        store=Store(self.root/'workspace.sqlite3');ledger=Ledger(store)
        j1,_=build_job(store,self.root,4);j2,_=build_job(store,self.root,5)
        plans=[prepare(ledger,self.root,j,'boss_greeting')['id'] for j in (j1,j2)]
        with patch('job_agent.delivery_worker.adapter_for',return_value=adapter):
            results=run_actions(self.root,plans)
        self.assertEqual(len(results),1);self.assertEqual(results[0]['status'],'batch_stopped')
        self.assertEqual(ledger.action(plans[1])['status'],'planned')


def profile_fixture():
    return {'id':'synthetic','url':'https://forms.example.test/apply','company':'示例公司','title':'AI实习生','reviewed':True,
            'identity_source_url':'https://company.example.test/jobs','identity_evidence_quote':'示例公司招聘 AI实习生',
            'company_selector':{'id':'company'},'title_selector':{'id':'title'},'fields':{'name':{'type':'Edit','id':'name'}},
            'upload_selector':{'id':'resume'},'attachment_selector':{'id':'attachment'},'submit_selector':{'id':'submit'},
            'receipt_selector':{'id':'receipt'},'receipt_prefix':'申请成功，编号：'}


class AdapterTests(unittest.TestCase):
    def test_profile_rejects_traversal_unknown_answers_and_regex(self):
        p=profile_fixture();validate_profile(p)
        for key,value in [('id','../../secret'),('fields',{'salary':{'id':'salary'}}),('url','http://localhost/apply')]:
            bad={**p,key:value}
            with self.assertRaises(DeliveryBlocked):validate_profile(bad)
        with self.assertRaises(DeliveryBlocked):validate_profile({**p,'receipt_pattern':'.*'})

    def test_chat_destination_rejects_other_domains_paths_and_credentials(self):
        adapter=BossAdapter(Path('.'))
        self.assertTrue(adapter.chat_destination('https://www.zhipin.com/web/geek/chat?bossId=test'))
        for target in ['https://other.test/web/geek/chat','https://www.zhipin.com/web/geek/chat/evil','https://user'+'@www.zhipin.com/web/geek/chat','http://www.zhipin.com/web/geek/chat']:
            self.assertFalse(adapter.chat_destination(target))

    def test_receipt_must_match_exact_job_and_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'job-specific.docx';path.write_bytes(b'fixture')
            a={'kind':'external_submit','payload':{'job_url':'https://job.test/1','company':'示例公司','title':'AI实习生'}}
            e={'kind':'external_submit','job_url':a['payload']['job_url'],'company':'示例公司','title':'AI实习生',
               'receipt_id':'TEST-123','attachment_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'attachment_name':path.name}
            adapter=FormAdapter(Path(d),profile_fixture(),{})
            self.assertTrue(adapter.verify(a,e,path))
            for key,value in [('job_url','https://job.test/2'),('receipt_id',''),('attachment_name','default.pdf'),('attachment_sha256','wrong')]:
                self.assertFalse(adapter.verify(a,{**e,key:value},path))
            boss=BossAdapter(Path(d));a={'kind':'boss_greeting','payload':{'job_url':'x','greeting':'hello'}}
            e={'kind':'boss_greeting','job_url':'x','message':'hello','delivery_status':'accepted','new_message_count':1}
            self.assertTrue(boss.verify(a,e,path));self.assertFalse(boss.verify(a,{**e,'new_message_count':0},path))

if __name__=='__main__':unittest.main()
