"""Exercise the BossAdapter UIA interaction logic on a synthetic loopback chat.

Uses a test-only adapter subclass to admit loopback URLs. Identity checks use the production adapter.
Never contacts a real recruiter or uses applicant material or the product ledger.
"""
from __future__ import annotations
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit

from job_agent.browser_delivery import BossAdapter,VisibleBrowser
from job_agent.delivery import DeliveryBlocked
from job_agent.settings import PRIVATE,atomic_write
from job_agent.store import utc_now

STYLE='<meta charset="utf-8"><title>本地合成聊天验证</title><style>body{font:18px Arial;margin:30px}button,input{font:18px Arial;margin:10px}li{margin:20px;padding:8px;border:1px solid #aaa}</style>'
DETAIL=STYLE+'''<h1>本地合成聊天验证 · 不联系招聘方</h1>
<section class="info-primary" role="group" aria-label="目标岗位"><h2>AI应用实习生</h2><p>示例测试公司</p></section>
<a class="btn btn-startchat" href="/web/geek/chat">立即沟通</a>
<section role="group" aria-label="公司"><h2>公司基本信息</h2><p>示例测试公司</p><a href="#jobs">查看全部职位</a></section>'''
CHAT=STYLE+'''<h1>本地合成会话</h1><section id="job-card" role="group" aria-label="会话岗位"><a id="job-link" href="https://www.zhipin.com/job_detail/synthetic.html">AI应用实习生</a><p>示例测试公司</p></section>
<ul id="messages"><li class="message-item item-friend"><p>请发一份简历给我</p><span class="message-time" role="group" aria-label="消息时间">MESSAGE_SENT_AT</span></li></ul>
<div id="chat-input" class="chat-input" contenteditable="true" role="textbox" aria-label="聊天输入" style="border:1px solid #333;min-height:45px"></div>
<input id="upload" type="file" accept=".docx"><span id="preview"></span><button id="send">发送</button>
<script>
const file=document.getElementById('upload');
file.onchange=()=>document.getElementById('preview').textContent=file.files[0]?.name||'';
document.getElementById('send').onclick=async()=>{
const input=document.getElementById('chat-input');let payload={kind:'greeting',message:input.innerText};
if(file.files.length){const bytes=await file.files[0].arrayBuffer();payload={kind:'resume',filename:file.files[0].name,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(v=>v.toString(16).padStart(2,'0')).join('')};}
await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
const item=document.createElement('li');item.className='message-item item-myself';
const body=document.createElement('p');body.textContent=payload.filename||payload.message;item.appendChild(body);
const status=document.createElement('span');status.className='message-status status-delivery';status.setAttribute('role','status');status.setAttribute('aria-label','送达');status.textContent='送达';item.appendChild(status);
document.getElementById('messages').appendChild(item);input.innerText='';file.value='';document.getElementById('preview').textContent='';};
</script>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,default=PRIVATE/'selfcheck'/'chat-ui');args=parser.parse_args()
    directory=args.output_dir;directory.mkdir(parents=True,exist_ok=True);posts=[];message_time=utc_now()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            content=(CHAT.replace('MESSAGE_SENT_AT',message_time) if self.path=='/web/geek/chat' else DETAIL).encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        def do_POST(self):
            if self.path!='/send':self.send_error(404);return
            size=int(self.headers.get('Content-Length','0'))
            if size>8192:self.send_error(413);return
            posts.append(json.loads(self.rfile.read(size)))
            content=b'{"ok":true}';self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    class LocalAdapter(BossAdapter):
        adapter_id='local-synthetic-chat'
        def validate_target(self,value):
            if value not in {origin+'/job_detail/synthetic.html',origin+'/web/geek/chat'}:raise DeliveryBlocked('wrong local target')
        def chat_destination(self,value):return value==origin+'/web/geek/chat'
    result={'status':'running','synthetic_only':True,'public_messages':0,'public_resume_sends':0}
    atomic_write(directory/'report.json',json.dumps(result).encode())
    try:
        from docx import Document
        attachment=directory/'resume-synthetic-chat.docx';doc=Document();doc.add_paragraph('Synthetic resume.');doc.save(attachment)
        job=origin+'/job_detail/synthetic.html'
        action={'id':'synthetic-greeting','kind':'boss_greeting','target_url':job,
                'payload':{'company':'示例测试公司','title':'AI应用实习生','job_url':'https://www.zhipin.com/job_detail/synthetic.html','greeting':'您好，这是本地测试消息，不联系招聘方。'}}
        subprocess.Popen(['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',job],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        time.sleep(1)
        adapter=LocalAdapter(directory,browser_factory=partial(VisibleBrowser,allow_local_test=True))
        adapter.inspect(action);assert not posts
        evidence=adapter.execute(action,attachment,lambda:None)
        assert adapter.verify(action,evidence,attachment)
        assert len(posts)==1 and posts[0]['message']==action['payload']['greeting']
        action={'id':'synthetic-resume','job_id':'synthetic','kind':'resume_send','target_url':origin+'/web/geek/chat',
                'payload':{**action['payload'],'reply_text':'请发一份简历给我','reply_sent_at':message_time}}
        reply,path=adapter.capture_reply(action)
        assert reply==action['payload']['reply_text'] and path.is_file() and len(posts)==1
        assert json.loads(path.read_text(encoding='utf-8'))['message_sent_at']==message_time
        adapter.inspect(action);assert len(posts)==1
        evidence=adapter.execute(action,attachment,lambda:None)
        assert adapter.verify(action,evidence,attachment)
        assert len(posts)==2 and posts[1]['filename']==attachment.name and posts[1]['sha256']==hashlib.sha256(attachment.read_bytes()).hexdigest()
        try:adapter.execute(action,attachment,lambda:None)
        except DeliveryBlocked as exc:
            assert '不重复发送' in str(exc)
        else:raise AssertionError('duplicate attachment was not blocked')
        assert len(posts)==2
        adapter.browser.navigate(job)
        assert adapter.browser.url==job
        adapter.browser.navigate(origin+'/web/geek/chat')
        assert adapter.browser.url==origin+'/web/geek/chat' and len(posts)==2
        result.update(status='pass',local_messages=1,local_resume_sends=1,message_matches=True,upload_hash_matches=True,
                      duplicate_attachment_blocked=True,bound_navigation_verified=True,
                      delivery_status_verified=True,reply_read_only=True,message_time_verified=True,production_identity_checks=True,preflight_read_only=True,screenshot=evidence['screenshot'])
    except Exception as exc:
        result.update(status='failed',error_type=type(exc).__name__,reason=str(exc),local_events=len(posts));raise
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
        atomic_write(directory/'report.json',json.dumps(result,ensure_ascii=False,indent=2).encode());print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
