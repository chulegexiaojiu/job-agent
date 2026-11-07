"""Workbench integration with isolated real children; no browser writes or network."""
import gc
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from job_agent.app import Workbench
from job_agent.delivery import prepare
from job_agent.delivery_ui import wait_for_delivery
from test_delivery import build_job


class DeliveryWindowTests(unittest.TestCase):
    def setUp(self):
        # Finalize old Tk cycles on the main thread before any new worker starts.
        gc.collect()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.private=Path(self.temp.name);self.root=tk.Tk();self.root.withdraw();self.app=Workbench(self.root,self.private)
        self.addCleanup(self.cleanup)
        self.job,_=build_job(self.app.store,self.private);self.app.refresh();self.app.jobs.selection_set(self.job)
    def cleanup(self):
        if not self.app.closed:self.app.close()
        thread=self.app.delivery_panel.worker_thread
        if thread:thread.join(timeout=20)
        self.app=None;self.root=None
        gc.collect()
    def pump(self,predicate,timeout=15):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if not self.app.closed:self.root.update()
            if predicate():return
            time.sleep(.03)
        self.fail('UI worker did not finish in time')

    def test_source_update_button_invalidates_plan_without_starting_model(self):
        from PIL import Image
        panel=self.app.delivery_panel;panel.plan_greetings();action=panel.tree.selection()[0]
        image=self.private/'new-source.png';Image.new('RGB',(400,400),'white').save(image)
        self.app.url.set(self.app.store.job(self.job)['url'])
        self.app.jd.insert('1.0','Complete synthetic replacement JD')
        self.app.screenshot.set(str(image))
        with patch.object(self.app,'start') as start:
            self.app.replace_selected_source();start.assert_not_called()
        self.assertEqual(self.app.store.job(self.job)['status'],'queued')
        self.assertEqual(self.app.ledger.action(action)['status'],'blocked')
        self.assertIn('草稿 0',self.app.counts.get())
        self.assertIn('已归档旧来源',self.app.status.get())
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.ledger.counts()['today_reserved_jobs'],0)

    def test_plan_details_default_gate_and_real_worker_report(self):
        panel=self.app.delivery_panel
        panel.plan_greetings();ids=panel.tree.selection();self.assertEqual(len(ids),1)
        self.assertEqual(str(panel.live_button['state']),'disabled')
        with patch.object(self.app,'text_window') as view:
            panel.details();view.assert_called_once();self.assertIn('planned',view.call_args.args[1])
        # A missing unique form profile fails before a browser is opened. Exercise the actual CLI process/report.
        row=self.app.ledger.action(ids[0])
        with self.app.store.connect() as db:db.execute("UPDATE outbound SET kind='external_submit' WHERE id=?",(ids[0],))
        panel.execute(False);self.pump(lambda:not panel.running)
        self.assertEqual(self.app.ledger.action(ids[0])['status'],'blocked')
        self.assertEqual(self.app.ledger.counts()['today_reserved_jobs'],0)
        reports=list((self.private/'delivery-runs').glob('*/report.json'));self.assertEqual(len(reports),1)
        report=json.loads(reports[0].read_text(encoding='utf-8'));self.assertEqual(report['mode'],'dry_run')
        self.assertEqual(report['results'][0]['status'],'blocked');self.assertFalse(self.app.busy)

    def test_stop_button_and_close_reap_real_isolated_worker(self):
        # The child reserves only a synthetic record and waits for cancellation; it cannot access a browser.
        script=self.private/'child.py'
        script.write_text('''import sys,time\nfrom pathlib import Path\nfrom job_agent.store import Store\nfrom job_agent.delivery import Ledger,Policy\np=Path(sys.argv[1]);ledger=Ledger(Store(p/'workspace.sqlite3'))\nledger.reserve(sys.argv[2],Policy())\n(p/'ready').write_text('ready')\nwhile not Path(sys.argv[3]).exists():time.sleep(.05)\n''',encoding='utf-8')
        for close in (False,True):
            with self.subTest(close=close):
                if close:
                    job,_=build_job(self.app.store,self.private,2);self.app.refresh();self.app.jobs.selection_set(job)
                panel=self.app.delivery_panel;panel.plan_greetings();action=panel.tree.selection()[0]
                ready=self.private/'ready';ready.unlink(missing_ok=True)
                real_popen=subprocess.Popen;children=[]
                def spawn(args,**kw):
                    cancel=args[args.index('--cancel-file')+1]
                    kw['env']['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
                    child=real_popen([sys.executable,str(script),str(self.private),action,cancel],**kw);children.append(child);return child
                with patch('job_agent.delivery_ui.subprocess.Popen',side_effect=spawn):
                    panel.execute(False);self.pump(ready.exists)
                    if close:self.app.close()
                    else:panel.stop_button.invoke()
                    panel.worker_thread.join(timeout=10)
                    self.assertFalse(panel.worker_thread.is_alive());self.assertIsNotNone(children[0].poll())
                if not close:
                    self.pump(lambda:not panel.running)
                    self.assertIn('今日触达 1 / 120',self.app.counts.get())
                    self.assertIn('结果待核实 1',self.app.counts.get())
                self.assertEqual(self.app.ledger.action(action)['status'],'unknown')
        self.assertEqual(self.app.ledger.counts()['unknown'],2)

    def test_reply_capture_worker_registers_evidence_and_creates_only_plan(self):
        # Only UIA capture is replaced; the real CLI, evidence ledger and planner run.
        script=self.private/'capture_child.py'
        script.write_text('''import hashlib,json
from pathlib import Path
import job_agent.delivery_worker as worker
from job_agent.store import utc_now
class SyntheticCapture:
 def __init__(self,private):self.private=private
 def capture_reply(self,action):
  folder=self.private/'replies'/'synthetic';folder.mkdir(parents=True)
  image=folder/'result.png';image.write_bytes(b'synthetic screenshot')
  text='请发一份简历给我'
  evidence=folder/'receipt.json'
  evidence.write_text(json.dumps({'job_url':action['payload']['job_url'],'page_url':action['target_url'],'text':text,'captured_at':utc_now(),'message_sent_at':utc_now(),'screenshot':str(image),'screenshot_sha256':hashlib.sha256(image.read_bytes()).hexdigest()}),encoding='utf-8')
  return text,evidence
worker.BossAdapter=SyntheticCapture
raise SystemExit(worker.main())
''',encoding='utf-8')
        panel=self.app.delivery_panel
        target='https://www.zhipin.com/web/geek/chat?bossId=synthetic'
        panel.target.set(target)
        real_popen=subprocess.Popen
        def spawn(args,**kw):
            kw['env']['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
            return real_popen([sys.executable,str(script),*args[3:]],**kw)
        with patch('job_agent.delivery_ui.subprocess.Popen',side_effect=spawn):
            panel.capture_reply();self.pump(lambda:not panel.running)
        reports=list((self.private/'delivery-runs').glob('*/report.json'))
        report=json.loads(reports[0].read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'completed')
        self.assertEqual(report['classification'],'resume_requested')
        action=self.app.ledger.action(report['created_plan'])
        self.assertEqual(action['kind'],'resume_send');self.assertEqual(action['status'],'planned')
        self.assertEqual(action['target_url'],target)
        self.assertEqual(self.app.ledger.reply(report['reply_id'],self.job)['text'],'请发一份简历给我')
        self.assertTrue(panel.tree.exists(action['id']))
        self.assertEqual(self.app.ledger.counts()['today_reserved_resumes'],0)

    def test_capture_screen_limit_reaches_worker_and_controls_restore(self):
        # Real child parses the actual CLI arguments; it exits without opening a browser.
        script=self.private/'capture_args_child.py'
        script.write_text('''import argparse,json,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path);p.add_argument('--private',type=Path);p.add_argument('--limit',type=int);p.add_argument('--screens',type=int);p.add_argument('--known-file',type=Path)
a=p.parse_args();time.sleep(.2)
(a.private/'capture-args.json').write_text(json.dumps({'screens':a.screens,'limit':a.limit,'known_present':a.known_file.is_file()}),encoding='utf-8')
print(json.dumps({'ok':False,'error':'synthetic capture completed without browser'}))
''',encoding='utf-8')
        real_popen=subprocess.Popen
        def spawn(args,**kw):return real_popen([sys.executable,str(script),*args[3:]],**kw)
        self.app.capture_screens.set('4');self.app.capture_limit.set('2')
        with patch('job_agent.app.subprocess.Popen',side_effect=spawn):
            self.app.import_browser(batch=True)
            self.assertEqual(str(self.app.screens_box['state']),'disabled')
            self.pump(lambda:not self.app.capturing)
        self.assertEqual(str(self.app.screens_box['state']),'readonly')
        result=json.loads((self.private/'capture-args.json').read_text(encoding='utf-8'))
        self.assertEqual(result,{'screens':4,'limit':2,'known_present':True})
        self.assertEqual(self.app.ledger.counts()['today_reserved_jobs'],0)

    def test_timeout_kills_unresponsive_child_and_retains_cancel_marker(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        cancel=self.private/'cancel-timeout'
        reason=wait_for_delivery(child,.1,lambda:False,cancel,grace_seconds=.1)
        self.assertEqual(reason,'timeout');self.assertTrue(cancel.exists());self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdout.closed);self.assertTrue(child.stderr.closed)

if __name__=='__main__':unittest.main()
