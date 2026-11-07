"""Outbound queue UI. Defaults to read-only inspection, never auto-arms live mode."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from uuid import uuid4
import webbrowser

from job_agent.delivery import DeliveryBlocked, prepare
from job_agent.settings import ROOT, atomic_write

LABELS={'planned':'待预检','previewed':'预检通过 · 未发送','blocked':'已暂停','reserved':'已预占额度',
        'executing':'执行中','succeeded':'成功凭据已保存','unknown':'结果待核实 · 不重试'}
KINDS={'boss_greeting':'首次开聊','resume_send':'回复后发送简历','external_submit':'外链网申'}


def wait_for_delivery(process, timeout, cancelled, cancel_file, *, grace_seconds=12):
    """Signal cooperative cancellation, then terminate and reap an unresponsive child."""
    deadline=time.monotonic()+timeout;grace=None;reason=''
    try:
        while True:
            stopping=cancelled() or time.monotonic()>deadline
            if stopping and grace is None:
                reason='cancelled' if cancelled() else 'timeout'
                atomic_write(cancel_file,b'cancel');grace=time.monotonic()+grace_seconds
            if grace is not None and time.monotonic()>grace:break
            try:
                process.communicate(timeout=.25)
                return reason
            except subprocess.TimeoutExpired:pass
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate(timeout=5)
        if process.stdout:process.stdout.close()
        if process.stderr:process.stderr.close()
    return reason


class DeliveryPanel:
    def __init__(self,app):
        self.app=app;self.running=False;self.worker_thread=None
        self.tab=ttk.Frame(app.tabs,padding=12)
        app.tabs.add(self.tab,text='沟通与网申')
        ttk.Label(self.tab,text='投递计划与执行记录',font=('Microsoft YaHei UI',14,'bold')).pack(anchor='w')
        ttk.Label(self.tab,text='默认只读预检。开聊、简历发送、网申分别记录；结果不明不自动重试。真实执行须本机策略已启用对应站点。',wraplength=1000).pack(anchor='w',pady=8)
        row=ttk.Frame(self.tab);row.pack(fill='x')
        ttk.Button(row,text='为所选岗位生成开聊计划',command=self.plan_greetings).pack(side='left')
        ttk.Button(row,text='导入网申站点配置',command=self.import_profile).pack(side='left',padx=6)
        ttk.Button(row,text='生成所选岗位网申计划',command=self.plan_form).pack(side='left')
        linkrow=ttk.Frame(self.tab);linkrow.pack(fill='x',pady=8)
        ttk.Label(linkrow,text='当前会话 / 网申链接').pack(side='left')
        self.target=tk.StringVar();ttk.Entry(linkrow,textvariable=self.target).pack(side='left',fill='x',expand=True,padx=8)
        ttk.Button(linkrow,text='读取招聘方回复',command=self.capture_reply).pack(side='left')
        self.tree=ttk.Treeview(self.tab,columns=('company','title','kind','status'),show='headings',height=10)
        for key,label,width in [('company','公司',180),('title','岗位',270),('kind','动作',145),('status','执行状态',240)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width)
        self.tree.pack(fill='both',expand=True)
        self.tree.bind('<Double-1>',lambda event:self.details())
        controls=ttk.Frame(self.tab);controls.pack(fill='x',pady=8)
        self.preview_button=ttk.Button(controls,text='只读预检所选计划',command=lambda:self.execute(False));self.preview_button.pack(side='left')
        self.live_button=ttk.Button(controls,text='执行已启用的所选计划',command=lambda:self.execute(True));self.live_button.pack(side='left',padx=6)
        ttk.Button(controls,text='打开所选目标页面',command=self.open_target).pack(side='left')
        ttk.Button(controls,text='查看执行记录',command=self.details).pack(side='left',padx=6)
        self.stop_button=ttk.Button(controls,text='停止执行',command=app.cancel,state='disabled');self.stop_button.pack(side='right')
        self.info=tk.StringVar(value='需要先在“岗位与分析”页选中已有有效草稿的岗位。')
        ttk.Label(self.tab,textvariable=self.info,wraplength=1000).pack(fill='x')

    def idle(self):
        if self.app.busy or self.app.capturing:
            self.info.set('请等待当前任务结束后再操作。');return False
        return True

    def refresh(self):
        selected=self.tree.selection();self.tree.delete(*self.tree.get_children())
        for row in self.app.ledger.actions():
            job=self.app.store.job(row['job_id'])
            self.tree.insert('','end',iid=row['id'],values=(job['company'],job['title'],KINDS[row['kind']],LABELS.get(row['status'],row['status'])))
        self.tree.selection_set([i for i in selected if self.tree.exists(i)])
        from job_agent.delivery_worker import load_policy
        try:policy=load_policy(self.app.private);live=policy.mode=='live' and bool(policy.enabled_adapters)
        except Exception:live=False
        idle=not self.running and not self.app.busy and not self.app.capturing
        self.preview_button.configure(state='normal' if idle else 'disabled')
        self.live_button.configure(state='normal' if live and idle else 'disabled')

    def plan_greetings(self):
        if not self.idle():return
        made=[];errors=[]
        for job_id in self.app.jobs.selection():
            try:made.append(prepare(self.app.ledger,self.app.private,job_id,'boss_greeting')['id'])
            except Exception as exc:errors.append(str(exc) if isinstance(exc,DeliveryBlocked) else '岗位草稿结构不完整。')
        self.refresh();self.tree.selection_set(made)
        self.info.set(f'生成或复用 {len(made)} 个计划，暂停 {len(errors)} 个。未开聊。'+(' '+errors[0] if errors else ''))

    def plan_form(self):
        if not self.idle():return
        selected=self.app.jobs.selection()
        if len(selected)!=1:self.info.set('请先在岗位页选中一个岗位。');return
        try:
            plan=prepare(self.app.ledger,self.app.private,selected[0],'external_submit',target_url=self.target.get().strip())
            self.refresh();self.tree.selection_set(plan['id']);self.info.set('已生成网申计划；先打开目标网页，再只读预检。')
        except Exception as exc:self.info.set(str(exc) if isinstance(exc,DeliveryBlocked) else '网申计划生成失败。')

    def import_profile(self):
        if not self.idle():return
        name=filedialog.askopenfilename(title='导入本机审核的表单配置',filetypes=[('JSON','*.json')],parent=self.app.root)
        if not name:return
        try:
            path=Path(name)
            if path.stat().st_size>65536:raise ValueError()
            data=json.loads(path.read_text(encoding='utf-8'))
            from job_agent.form_profile import validate_profile
            validate_profile(data)
            dest=self.app.private/'form-profiles'/(data['id']+'.json')
            atomic_write(dest,json.dumps(data,ensure_ascii=False,indent=2).encode())
            self.info.set('已保存站点配置。导入不会启用真实执行；预检仍会核验来源、字段与岗位。')
        except Exception:self.info.set('配置不符合支持的单页表单结构；请参照 README。')

    def capture_reply(self):
        if not self.idle():return
        selected=self.app.jobs.selection()
        if len(selected)!=1:self.info.set('请先在岗位页选中一个岗位。');return
        self.launch({'operation':'capture_reply','job_id':selected[0],'thread_url':self.target.get().strip()},False)

    def execute(self,live):
        if not self.idle():return
        selected=list(self.tree.selection())
        if not selected:self.info.set('请先选择执行计划。');return
        self.launch({'operation':'actions','ids':selected},live)

    def launch(self,request,live):
        app=self.app
        directory=app.private/'delivery-runs'/uuid4().hex
        req=directory/'request.json';report=directory/'report.json';cancel=directory/'cancel'
        atomic_write(req,json.dumps(request,ensure_ascii=False).encode())
        app.cancelled.clear();app.busy=True;self.running=True
        self.preview_button.configure(state='disabled');self.live_button.configure(state='disabled');self.stop_button.configure(state='normal')
        app.cancel_button.configure(state='normal')
        self.info.set('正在执行已启用计划；不要切换浏览器或操作页面。' if live else '正在只读预检 / 读取回复；不会填写、上传、发送或提交。')
        def worker():
            python=Path(sys.executable).with_name('python.exe') if os.name=='nt' else Path(sys.executable)
            args=[str(python),'-m','job_agent.delivery_worker','--private',str(app.private),'--request',str(req),'--report',str(report),'--cancel-file',str(cancel)]
            if live:args.append('--live')
            process=None
            try:
                process=subprocess.Popen(args,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',
                    env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                wait_for_delivery(process,max(60,90*len(request.get('ids',[1]))),app.cancelled.is_set,cancel)
                result=str(report)
            except Exception:
                result=''
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.communicate(timeout=5)
                if process is not None:
                    if process.stdout:process.stdout.close()
                    if process.stderr:process.stderr.close()
                try:app.ledger.recover()
                finally:app.events.put(('delivery_done',result))
        self.worker_thread=threading.Thread(target=worker,daemon=False)
        self.worker_thread.start()

    def finish(self,report):
        self.running=False;self.app.busy=False
        self.preview_button.configure(state='normal');self.stop_button.configure(state='disabled');self.app.cancel_button.configure(state='disabled')
        try:
            data=json.loads(Path(report).read_text(encoding='utf-8'))
            reason=data.get('reason','')
            message='任务已结束；请双击计划查看逐项状态。'+reason
            if data.get('classification'):message+=' 回复分类：'+data['classification']
            if data.get('status')=='running':message='执行进程已停止；可能生效的动作已标记“结果待核实”。'
        except Exception:message='执行工作进程未正常完成；请查看计划状态，结果不明不会自动重试。'
        self.info.set(message);self.app.status.set(message);self.app.refresh()

    def open_target(self):
        if not self.idle():return
        selected=self.tree.selection()
        if len(selected)==1:webbrowser.open(self.app.ledger.action(selected[0])['target_url'])

    def details(self):
        selected=self.tree.selection()
        if len(selected)!=1:return
        row=self.app.ledger.action(selected[0])
        body={key:row[key] for key in ('kind','status','target_url','reason','reserved_day','evidence')}
        self.app.text_window('执行记录',json.dumps(body,ensure_ascii=False,indent=2))
