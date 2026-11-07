"""Opt-in real Edge/UIA check against a synthetic loopback-only application form.

No API key, applicant material, recruiter, public form or production ledger is used.
Runs a temporary HTTP server; closes it on completion. Browser test tab can remain.
"""
from __future__ import annotations
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from job_agent.browser_delivery import FormAdapter, VisibleBrowser
from job_agent.settings import PRIVATE, atomic_write

PAGE='''<!doctype html><meta charset="utf-8"><title>本地合成网申验证</title>
<style>body{font:18px Arial;margin:25px;max-width:920px}label{display:block;margin:12px}input{font:18px Arial}button{font:20px Arial;margin:12px}</style>
<h1>本地合成网申验证 · 不访问招聘方</h1>
<div role="group" aria-label="示例测试公司" id="company">示例测试公司</div>
<div role="group" aria-label="AI应用实习生" id="title">AI应用实习生</div>
<form id="form" autocomplete="off">
<label>姓名<input id="name" aria-label="姓名" autocomplete="off" required></label>
<label>邮箱<input id="email" aria-label="邮箱" autocomplete="off" required></label>
<input type="file" id="resume" aria-label="测试附件" accept=".docx" required>
<div role="group" id="attachment" aria-label="无附件">无附件</div>
<button id="submit" type="submit">提交本地测试</button></form>
<div id="result"></div>
<script>
const file=document.getElementById('resume');
file.onchange=()=>{let n=file.files[0]?.name||'无附件';const a=document.getElementById('attachment');a.textContent=n;a.setAttribute('aria-label',n)};
document.getElementById('form').onsubmit=async(e)=>{e.preventDefault();
const bytes=new Uint8Array(await file.files[0].arrayBuffer());
const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(v=>v.toString(16).padStart(2,'0')).join('');
const response=await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('name').value,email:document.getElementById('email').value,filename:file.files[0].name,sha256:hash})});
const data=await response.json();const result=document.getElementById('result');
result.innerHTML='<div id="receipt" role="group" aria-label="申请成功，编号：'+data.receipt+'">申请成功，编号：'+data.receipt+'</div>';};
</script>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,default=PRIVATE/'selfcheck'/'delivery-ui')
    args=parser.parse_args();directory=args.output_dir;directory.mkdir(parents=True,exist_ok=True)
    posts=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            content=PAGE.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        def do_POST(self):
            if self.path!='/submit':self.send_error(404);return
            length=int(self.headers.get('Content-Length','0'))
            if length>8192:self.send_error(413);return
            posts.append(json.loads(self.rfile.read(length)))
            content=json.dumps({'receipt':'LOCAL-'+uuid4().hex[:12]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}/'
    result={'status':'running','synthetic_only':True,'public_submissions':0}
    atomic_write(directory/'report.json',json.dumps(result).encode())
    try:
        attachment=directory/'resume-synthetic-unique.docx'
        from docx import Document
        doc=Document();doc.add_paragraph('Synthetic applicant for loopback UI tests only.');doc.save(attachment)
        subprocess.Popen(['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',url],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        profile={'id':'local-test','url':url,'company':'示例测试公司','title':'AI应用实习生','reviewed':True,
          'identity_source_url':url,'identity_evidence_quote':'本地合成网申验证',
          'company_selector':{'id':'company'},'title_selector':{'id':'title'},'fields':{'name':{'id':'name'},'email':{'id':'email'}},
          'upload_selector':{'id':'resume'},'attachment_selector':{'id':'attachment'},'submit_selector':{'id':'submit'},
          'receipt_selector':{'id':'receipt'},'receipt_prefix':'申请成功，编号：'}
        action={'id':'synthetic-loopback','kind':'external_submit','target_url':url,
          'payload':{'company':profile['company'],'title':profile['title'],'job_url':url}}
        adapter=FormAdapter(directory,profile,{'name':'Synthetic Applicant','email':'synthetic@example.test'},
          browser_factory=partial(VisibleBrowser,allow_local_test=True))
        time.sleep(1)
        plan=adapter.inspect(action)
        assert not posts
        evidence=adapter.execute(action,attachment,lambda:None)
        assert adapter.verify(action,evidence,attachment)
        assert len(posts)==1 and posts[0]['filename']==attachment.name
        assert posts[0]['sha256']==hashlib.sha256(attachment.read_bytes()).hexdigest()
        assert posts[0]['name']=='Synthetic Applicant' and posts[0]['email']=='synthetic@example.test'
        result.update(status='pass',preflight_read_only=True,local_submissions=len(posts),upload_hash_matches=True,
                      input_values_match=True,receipt_verified=True,screenshot=evidence['screenshot'])
    except Exception as exc:
        result.update(status='failed',error_type=type(exc).__name__,reason=str(exc),local_submissions=len(posts))
        raise
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
        atomic_write(directory/'report.json',json.dumps(result,ensure_ascii=False,indent=2).encode())
        print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
