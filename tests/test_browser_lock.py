"""Real OS-lock contention across isolated capture and delivery processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from job_agent.browser_lock import browser_session_lock
from job_agent.delivery import BatchStopped
from job_agent.delivery_worker import run_actions

ROOT=Path(__file__).resolve().parents[1]


class BrowserLockTests(unittest.TestCase):
    def test_capture_and_delivery_contend_then_process_exit_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)
            code=("import sys,time;from pathlib import Path;"
                  "from job_agent.browser_lock import browser_session_lock;"
                  "lock=browser_session_lock(Path(sys.argv[1]));lock.__enter__();"
                  "print('ready',flush=True);time.sleep(30)")
            child=subprocess.Popen([sys.executable,'-c',code,str(private)],cwd=ROOT,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                                   env={**os.environ,'PYTHONIOENCODING':'utf-8'})
            try:
                self.assertEqual(child.stdout.readline().strip(),'ready')
                with self.assertRaises(BatchStopped):run_actions(private,[])
                known=private/'known.json';known.write_text('[]',encoding='utf-8')
                for module,extra in [('job_agent.browser_capture',[]),
                                     ('job_agent.browser_batch',['--known-file',str(known),'--screens','3'])]:
                    with self.subTest(module=module):
                        output=private/module
                        result=subprocess.run([sys.executable,'-m',module,'--private',str(private),
                                               '--output-dir',str(output),*extra],cwd=ROOT,
                                              capture_output=True,text=True,encoding='utf-8',timeout=10,
                                              env={**os.environ,'PYTHONIOENCODING':'utf-8'})
                        self.assertEqual(result.returncode,1)
                        response=json.loads(result.stdout)
                        self.assertFalse(response['ok']);self.assertIn('另一个浏览器',response['error'])
                        self.assertFalse(output.exists())
            finally:
                if child.poll() is None:child.terminate()
                child.communicate(timeout=5)
                child.stdout.close();child.stderr.close()
            with browser_session_lock(private):pass


if __name__=='__main__':unittest.main()
