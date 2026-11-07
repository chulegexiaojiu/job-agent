"""Local job workbench. Outbound execution defaults to dry-run with local policy gates."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from uuid import uuid4
from tkinter import filedialog, messagebox, ttk
import webbrowser

from PIL import ImageGrab

from job_agent.client import ModelClient
from job_agent.materials import ingest, read_identity
from job_agent.pipeline import Pipeline
from job_agent.settings import PRIVATE, ROOT, atomic_write, load_connection
from job_agent.store import Store

STATUSES = {'queued': '待分析', 'running': '分析中', 'paused': '待核实', 'error': '异常暂停',
            'cancelled': '已停止', 'facts_required': '待确认素材', 'draft_ready': '草稿就绪'}


class Workbench:
    def __init__(self, root: tk.Tk, private: Path = PRIVATE):
        self.root, self.private = root, private
        self.store = Store(private / 'workspace.sqlite3')
        self.store.recover_interrupted()
        from job_agent.delivery import Ledger
        self.ledger = Ledger(self.store)
        self.ledger.recover()
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.busy = self.closed = self.capturing = False
        root.title('本地求职助手 · 不发送验证')
        root.geometry('1120x800')
        root.minsize(940, 720)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('WorkbenchTitle.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Treeview', rowheight=27)
        shell = ttk.Frame(root, padding=18)
        shell.pack(fill='both', expand=True)
        ttk.Label(shell, text='岗位核验与简历工作台', style='WorkbenchTitle.TLabel').pack(anchor='w')
        self.mode_label=tk.StringVar(value='当前为不发送验证：岗位质量与个人匹配分开评分；先研究公司，再选择真实素材。')
        ttk.Label(shell, textvariable=self.mode_label).pack(anchor='w', pady=(6, 8))
        try:
            connection = load_connection(private / 'connection.json')
            label = f'模型：{connection.model}    接口：{connection.base_url}' if connection else '请先配置 API'
        except ValueError:
            label = 'API 配置需要修复'
        top = ttk.Frame(shell)
        top.pack(fill='x', pady=(0, 8))
        ttk.Label(top, text=label).pack(side='left')
        ttk.Button(top, text='API 配置 / 复测', command=self.open_setup).pack(side='right')
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill='both', expand=True)
        self.job_tab, self.fact_tab = ttk.Frame(self.tabs, padding=12), ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.job_tab, text='岗位与分析')
        self.tabs.add(self.fact_tab, text='真实素材库')
        self.build_jobs()
        self.build_facts()
        from job_agent.delivery_ui import DeliveryPanel
        self.delivery_panel = DeliveryPanel(self)
        self.status = tk.StringVar(value='导入岗位文字和截图后，点击“分析待办岗位”。未确认素材不会进入简历。')
        ttk.Label(shell, textvariable=self.status, wraplength=1060).pack(side='bottom', before=self.tabs, fill='x', pady=(8, 0))
        self.refresh()
        if self.store.facts() and not self.store.facts(confirmed_only=True):
            self.tabs.select(self.fact_tab)
        self.poll_id = root.after(100, self.drain)

    def build_jobs(self):
        tab = self.job_tab
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text='岗位链接').grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.url = tk.StringVar()
        ttk.Entry(tab, textvariable=self.url).grid(row=0, column=1, sticky='ew')
        ttk.Button(tab, text='批量导入 JSON', command=self.import_batch).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(tab, text='网页文字').grid(row=1, column=0, sticky='nw', pady=8)
        self.jd = tk.Text(tab, height=5, wrap='word', font=('Microsoft YaHei UI', 10))
        self.jd.grid(row=1, column=1, columnspan=2, sticky='ew', pady=8)
        shot_row = ttk.Frame(tab)
        shot_row.grid(row=2, column=0, columnspan=3, sticky='ew')
        self.screenshot = tk.StringVar()
        self.capture_button = ttk.Button(shot_row, text='导入当前浏览器岗位页', command=self.import_browser)
        self.capture_button.pack(side='left', padx=(0, 6))
        ttk.Button(shot_row, text='选择岗位截图', command=self.choose_image).pack(side='left')
        ttk.Button(shot_row, text='粘贴剪贴板截图', command=self.clipboard_image).pack(side='left', padx=6)
        ttk.Label(shot_row, textvariable=self.screenshot, width=60).pack(side='left', fill='x', expand=True)
        ttk.Button(shot_row, text='加入待办', command=self.add_job).pack(side='right')
        batch_row = ttk.Frame(tab)
        batch_row.grid(row=3, column=0, columnspan=3, sticky='ew', pady=8)
        self.batch_capture_button = ttk.Button(batch_row, text='自动采集当前列表', command=lambda: self.import_browser(batch=True))
        self.batch_capture_button.pack(side='left')
        ttk.Label(batch_row, text='每轮').pack(side='left', padx=(8, 3))
        self.capture_limit = tk.StringVar(value='3')
        self.limit_box = ttk.Spinbox(batch_row, from_=1, to=5, width=3, textvariable=self.capture_limit, state='readonly')
        self.limit_box.pack(side='left')
        ttk.Label(batch_row, text='个；最多浏览').pack(side='left', padx=6)
        self.capture_screens = tk.StringVar(value='1')
        self.screens_box = ttk.Spinbox(batch_row, from_=1, to=20, width=3, textvariable=self.capture_screens, state='readonly')
        self.screens_box.pack(side='left')
        ttk.Label(batch_row, text='屏 · 1=当前可见；自动翻动列表，去重后分析，不发送。').pack(side='left', padx=6)
        tools = ttk.Frame(tab)
        tools.grid(row=4, column=0, columnspan=3, sticky='ew')
        self.run_button = ttk.Button(tools, text='分析待办岗位', command=self.start_batch)
        self.run_button.pack(side='left')
        self.selected_button = ttk.Button(tools, text='继续所选岗位', command=self.start_selected)
        self.selected_button.pack(side='left', padx=6)
        self.cancel_button = ttk.Button(tools, text='停止采集 / 分析', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left')
        ttk.Button(tools, text='查看所选结果', command=self.show_result).pack(side='right')
        ttk.Button(tools, text='用上方内容更新所选来源', command=self.replace_selected_source).pack(side='right', padx=6)
        self.counts = tk.StringVar()
        ttk.Label(tab, textvariable=self.counts).grid(row=5, column=0, columnspan=3, sticky='w', pady=8)
        self.jobs = ttk.Treeview(tab, columns=('company', 'title', 'status', 'quality', 'match'), show='headings', height=7)
        for key, label, width in [('company', '公司', 180), ('title', '岗位', 270), ('status', '状态', 200), ('quality', '岗位质量', 85), ('match', '个人匹配', 85)]:
            self.jobs.heading(key, text=label)
            self.jobs.column(key, width=width, stretch=key in {'company', 'title'})
        self.jobs.grid(row=6, column=0, columnspan=3, sticky='nsew')
        job_scroll = ttk.Scrollbar(tab, orient='vertical', command=self.jobs.yview)
        job_scroll.grid(row=6, column=3, sticky='ns')
        self.jobs.configure(yscrollcommand=job_scroll.set)
        tab.rowconfigure(6, weight=1)
        self.jobs.bind('<Double-1>', lambda event: self.show_result())
        self.log = tk.Text(tab, height=4, wrap='word', state='disabled', font=('Microsoft YaHei UI', 9))
        self.log.grid(row=7, column=0, columnspan=3, sticky='ew', pady=(8, 0))

    def build_facts(self):
        tab = self.fact_tab
        ttk.Label(tab, text='来源文字不等于已证实事实。请选择确实发生且可以解释的条目，批量标记为真实；未确认的机构关系、业绩和指标不会进入草稿。', wraplength=1000).pack(anchor='w', pady=(0, 8))
        controls = ttk.Frame(tab)
        controls.pack(fill='x')
        ttk.Button(controls, text='导入 Word / PDF / 文本', command=self.import_material).pack(side='left')
        ttk.Button(controls, text='所选条目属实', command=lambda: self.confirm(True)).pack(side='left', padx=6)
        ttk.Button(controls, text='撤销确认', command=lambda: self.confirm(False)).pack(side='left')
        ttk.Button(controls, text='本地姓名与联系方式', command=self.edit_identity).pack(side='right')
        ttk.Label(tab, text='支持 Ctrl / Shift 多选，Ctrl+A 全选。项目条目确认包含其上方项目名称、身份与日期。双击查看原文和来源。').pack(anchor='w', pady=8)
        fact_frame = ttk.Frame(tab)
        fact_frame.pack(fill='both', expand=True)
        fact_frame.rowconfigure(0, weight=1)
        fact_frame.columnconfigure(0, weight=1)
        self.facts = ttk.Treeview(fact_frame, columns=('confirmed', 'section', 'text'), show='headings', selectmode='extended')
        for key, label, width in [('confirmed', '真实性', 90), ('section', '章节 / 项目', 280), ('text', '素材原文', 620)]:
            self.facts.heading(key, text=label)
            self.facts.column(key, width=width, stretch=key == 'text')
        self.facts.grid(row=0, column=0, sticky='nsew')
        fact_scroll = ttk.Scrollbar(fact_frame, orient='vertical', command=self.facts.yview)
        fact_scroll.grid(row=0, column=1, sticky='ns')
        fact_horizontal = ttk.Scrollbar(fact_frame, orient='horizontal', command=self.facts.xview)
        fact_horizontal.grid(row=1, column=0, sticky='ew')
        self.facts.configure(yscrollcommand=fact_scroll.set, xscrollcommand=fact_horizontal.set)
        self.facts.bind('<Double-1>', lambda event: self.show_fact())
        self.facts.bind('<Control-a>', self.select_all_facts)
        self.fact_count = tk.StringVar()
        ttk.Label(tab, textvariable=self.fact_count).pack(anchor='w', pady=(8, 0))

    def select_all_facts(self, event=None):
        self.facts.selection_set(self.facts.get_children())
        return 'break'

    def refresh(self):
        selected_jobs, selected_facts = self.jobs.selection(), self.facts.selection()
        self.jobs.delete(*self.jobs.get_children())
        rows = self.store.jobs()
        for row in rows:
            self.jobs.insert('', 'end', iid=row['id'], values=(row['company'] or '待提取', row['title'] or row['url'][:90], STATUSES.get(row['status'], row['status']), row['quality'] if row['quality'] is not None else '—', row['match'] if row['match'] is not None else '—'))
        self.jobs.selection_set([item for item in selected_jobs if self.jobs.exists(item)])
        counts = self.ledger.counts()
        from job_agent.delivery_worker import load_policy
        try:policy=load_policy(self.private)
        except Exception:
            from job_agent.delivery import Policy
            policy=Policy()
        live=policy.mode=='live' and bool(policy.enabled_adapters)
        self.root.title('本地求职助手 · 已启用部分真实执行' if live else '本地求职助手 · 不发送验证')
        self.mode_label.set('已启用本机策略中的站点；真实执行仅由“沟通与网申”页启动。' if live else '当前为不发送验证：岗位质量与个人匹配分开评分；先研究公司，再选择真实素材。')
        self.counts.set(f'岗位 {len(rows)} · 草稿 {sum(r["status"] == "draft_ready" for r in rows)} · 今日触达 {counts["today_reserved_jobs"]} / {policy.daily_new_jobs} · 附件 {counts["today_reserved_resumes"]} / {policy.daily_resume_sends}\n累计开聊成功 {counts["boss_greeting"]} · 简历发送 {counts["resume_send"]} · 网申 {counts["external_submit"]} · 结果待核实 {counts["unknown"]}')
        if hasattr(self, 'delivery_panel'):
            self.delivery_panel.refresh()
        self.facts.delete(*self.facts.get_children())
        facts = self.store.facts()
        for fact in facts:
            self.facts.insert('', 'end', iid=fact['id'], values=('已确认' if fact['confirmed'] else '待确认', fact['section'], fact['text']))
        self.facts.selection_set([item for item in selected_facts if self.facts.exists(item)])
        self.fact_count.set(f'共 {len(facts)} 条，已确认 {sum(bool(f["confirmed"]) for f in facts)} 条。素材文件路径和联系方式不发给研究模型。')

    def choose_image(self):
        value = filedialog.askopenfilename(title='选择岗位区域截图', filetypes=[('图片', '*.png *.jpg *.jpeg *.webp')], parent=self.root)
        if value:
            self.screenshot.set(value)

    def clipboard_image(self):
        try:
            image = ImageGrab.grabclipboard()
            if not hasattr(image, 'save'):
                raise ValueError('剪贴板中没有图片。请先截取岗位区域。')
            path = self.private / 'clipboard-job.png'
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, 'PNG')
            self.screenshot.set(str(path))
        except Exception:
            self.status.set('无法读取剪贴板图片，请先截取岗位区域，或选择截图文件。')

    def insert_job(self, url: str, text: str, screenshot: str) -> tuple[str, bool]:
        from job_agent.client import image_data
        if screenshot:
            image_data(Path(screenshot))  # Validate before creating a queue entry.
        job_id, inserted = self.store.add_job(url, text)
        if inserted and screenshot:
            directory = self.private / 'jobs' / job_id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / ('source' + Path(screenshot).suffix.lower())
            shutil.copyfile(screenshot, target)
            self.store.update(job_id, screenshot=str(target.resolve()))
        return job_id, inserted

    def add_job(self):
        try:
            job_id, inserted = self.insert_job(self.url.get().strip(), self.jd.get('1.0', 'end').strip(), self.screenshot.get())
            self.refresh()
            self.jobs.selection_set(job_id)
            self.status.set('已加入待办。点击分析后自动核验与研究。' if inserted else '该岗位链接已存在，没有重复加入。')
            if inserted:
                self.url.set('')
                self.jd.delete('1.0', 'end')
                self.screenshot.set('')
        except (OSError, ValueError):
            self.status.set('导入失败：请检查完整岗位链接、文字及截图文件；详情格式见 README。')

    def replace_selected_source(self):
        if self.busy or self.capturing:
            self.status.set('请等待当前任务结束后再更新来源。')
            return
        selected = self.jobs.selection()
        if len(selected) != 1:
            self.status.set('请选中一个岗位，并在上方填写同一链接、完整文字及新截图。')
            return
        try:
            from job_agent.source_revision import replace_source
            replace_source(self.store, self.private, selected[0], self.url.get().strip(),
                           self.jd.get('1.0', 'end').strip(), Path(self.screenshot.get()))
            self.refresh()
            self.status.set('已归档旧来源并更新所选岗位；旧草稿和计划失效。请点击“继续所选岗位”重新核验。未发送。')
        except (OSError, ValueError) as exc:
            self.status.set(str(exc) if isinstance(exc, ValueError) else '来源更新失败，原始文件保留在本地。')

    def import_capture(self, manifest: Path, expected_url: str | None = None) -> tuple[str, bool]:
        from job_agent.browser_batch import read_capture
        from job_agent.browser_capture import detail_url
        capture = read_capture(manifest)
        if expected_url is not None and capture['url'] != detail_url(expected_url):
            raise ValueError('采集链接与批次目标不一致。')
        job_id, inserted = self.insert_job(capture['url'], capture['text'], capture['screenshot'])
        if inserted:
            source = self.private / 'jobs' / job_id / 'source-capture.json'
            atomic_write(source, json.dumps(capture, ensure_ascii=False, indent=2).encode())
            self.store.event(job_id, 'capture', '浏览器文字与截图哈希已核对并保存来源清单；未触达。')
        return job_id, inserted

    def import_browser(self, batch: bool = False):
        if self.busy or self.capturing:
            self.status.set('当前正在处理，请等待完成后再导入浏览器页面。')
            return
        try:
            limit = int(self.capture_limit.get()) if batch else 1
            screens = int(self.capture_screens.get()) if batch else 1
            if not 1 <= screens <= 20:
                raise ValueError()
            if not 1 <= limit <= 5:
                raise ValueError()
        except ValueError:
            self.status.set('每轮采集数量必须为 1～5，浏览上限必须为 1～20 屏。')
            return
        self.cancelled.clear()
        self.capturing = True
        self.capture_button.configure(state='disabled')
        self.batch_capture_button.configure(state='disabled')
        self.limit_box.configure(state='disabled')
        self.screens_box.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        self.run_button.configure(state='disabled')
        self.selected_button.configure(state='disabled')
        self.status.set('正在自动截图并导入；浏览器会切到前台，请勿切换标签或操作页面。可点“停止采集 / 分析”取消。')
        directory = self.private / 'captures' / uuid4().hex
        module = 'job_agent.browser_batch' if batch else 'job_agent.browser_capture'
        extra = []
        if batch:
            known = directory / 'known.json'
            atomic_write(known, json.dumps([row['url'] for row in self.store.jobs()]).encode())
            extra = ['--limit', str(limit), '--known-file', str(known), '--screens', str(screens)]
        def worker():
            python = Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)
            try:
                with subprocess.Popen([str(python), '-m', module, '--output-dir', str(directory), '--private', str(self.private)] + extra,
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, encoding='utf-8', errors='replace',
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                      env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}) as process:
                    from job_agent.browser_batch import wait_for_capture
                    last_progress = ''
                    def progress_tick():
                        nonlocal last_progress
                        if batch and (directory / 'batch.json').exists():
                            report = json.loads((directory / 'batch.json').read_text(encoding='utf-8'))
                            progress = report.get('progress', '')
                            if progress and progress != last_progress:
                                self.events.put(('progress', progress))
                                last_progress = progress
                    output, interruption = wait_for_capture(process, 20 + 45 * limit + 5 * screens if batch else 35,
                                                            self.cancelled.is_set, progress_tick)
                    if interruption:
                        interrupted = '已停止采集。' if interruption == 'cancelled' else '采集超时，已停止。'
                        if batch and (directory / 'batch.json').exists():
                            report = json.loads((directory / 'batch.json').read_text(encoding='utf-8'))
                            report.update(status=interruption, reason=interrupted)
                            atomic_write(directory / 'batch.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
                            self.events.put(('batch_capture_ready', str(directory / 'batch.json')))
                        else:
                            self.events.put(('capture_error', interrupted))
                        return
                    answer = json.loads(output.strip())
                    if process.returncode or not answer.get('ok'):
                        self.events.put(('capture_error', answer.get('error', '浏览器导入失败，请使用手动导入。')))
                    else:
                        self.events.put(('batch_capture_ready' if batch else 'capture_ready',
                                         str(directory / ('batch.json' if batch else 'capture.json'))))
            except Exception:
                self.events.put(('capture_error', '浏览器导入失败，请使用手动导入。'))
        # Non-daemon: closing the UI must let the worker terminate its browser child.
        threading.Thread(target=worker, daemon=False).start()

    def finish_capture(self):
        self.capturing = False
        self.capture_button.configure(state='normal')
        self.batch_capture_button.configure(state='normal')
        self.limit_box.configure(state='readonly')
        self.screens_box.configure(state='readonly')
        self.cancel_button.configure(state='disabled')
        self.run_button.configure(state='normal')
        self.selected_button.configure(state='normal')

    def import_batch(self):
        filename = filedialog.askopenfilename(title='选择岗位 JSON 清单', filetypes=[('JSON', '*.json')], parent=self.root)
        if not filename:
            return
        added, duplicates, failed = 0, 0, 0
        try:
            path = Path(filename)
            if path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError()
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, list) or len(data) > 1000:
                raise ValueError()
            for entry in data:
                try:
                    if not isinstance(entry, dict) or not isinstance(entry.get('url'), str) or not isinstance(entry.get('text'), str):
                        raise ValueError()
                    image = entry.get('screenshot', '')
                    if image:
                        image = str((path.parent / image).resolve())
                    _, inserted = self.insert_job(entry['url'], entry['text'], image)
                    added += int(inserted)
                    duplicates += int(not inserted)
                except (OSError, ValueError, TypeError):
                    failed += 1
            self.refresh()
            self.status.set(f'批量导入：新增 {added}，去重 {duplicates}，失败 {failed}。')
        except (OSError, ValueError):
            self.status.set('JSON 文件无效，格式示例见 README。')

    def import_material(self):
        files = filedialog.askopenfilenames(title='导入真实经历素材', filetypes=[('素材', '*.docx *.pdf *.txt *.md')], parent=self.root)
        count, failures = 0, []
        for filename in files:
            try:
                count += ingest(Path(filename), self.store, self.private)
            except Exception:
                failures.append(Path(filename).name)
        self.refresh()
        self.status.set(f'新增 {count} 条待确认素材。' + ('读取失败：' + '、'.join(failures) if failures else ''))

    def confirm(self, value: bool):
        if self.busy:
            self.status.set('请先停止分析并等待完成，再修改真实性确认。')
            return
        selected = list(self.facts.selection())
        if not selected:
            self.status.set('请先选择要确认或撤销的素材条目。')
            return
        self.store.confirm_facts(selected, value)
        self.refresh()
        self.status.set(f'已{"确认" if value else "撤销"} {len(selected)} 条素材；只会在下一次生成时使用当前确认状态。')

    def text_window(self, title: str, content: str):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry('960x720')
        text = tk.Text(window, wrap='word', font=('Microsoft YaHei UI', 10), padx=16, pady=16)
        scroll = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.insert('1.0', content)
        for i, match in enumerate(re.finditer(r'https?://[^\s\"<>]+', content)):
            tag = 'link' + str(i)
            text.tag_add(tag, f'1.0+{match.start()}c', f'1.0+{match.end()}c')
            text.tag_configure(tag, foreground='#17699b', underline=True)
            text.tag_bind(tag, '<Button-1>', lambda event, url=match.group(0): webbrowser.open(url))
        text.configure(state='disabled')
        return window

    def show_fact(self):
        selected = self.facts.selection()
        if selected:
            fact = next(f for f in self.store.facts() if f['id'] == selected[0])
            self.text_window('素材原文及来源', f'{fact["section"]}\n\n{fact["text"]}\n\n来源：{fact["source"]}\n位置：{fact["locator"]}\n真实性：' + ('已确认' if fact['confirmed'] else '待确认'))

    def edit_identity(self):
        values = read_identity(self.private)
        window = tk.Toplevel(self.root)
        window.title('本地个人资料')
        fields = {}
        for index, (name, label) in enumerate([('name', '姓名'), ('phone', '电话'), ('email', '邮箱')]):
            ttk.Label(window, text=label).grid(row=index, column=0, padx=12, pady=8)
            variable = tk.StringVar(value=values.get(name, ''))
            fields[name] = variable
            ttk.Entry(window, textvariable=variable, width=40).grid(row=index, column=1, padx=12, pady=8)
        def save():
            data = {name: field.get().strip() for name, field in fields.items()}
            if any(len(value) > 250 for value in data.values()):
                return
            atomic_write(self.private / 'identity.json', json.dumps(data, ensure_ascii=False, indent=2).encode())
            window.destroy()
        ttk.Label(window, text='仅本地保存与导出时回填，不发送给公司研究模型。').grid(row=3, column=0, columnspan=2, padx=12, pady=8)
        ttk.Button(window, text='保存', command=save).grid(row=4, column=1, sticky='e', padx=12, pady=12)

    def show_result(self):
        selected = self.jobs.selection()
        if not selected:
            return
        row = self.store.job(selected[0])
        detail = row['detail']
        lines = [row['company'] + ' / ' + row['title'], '状态：' + STATUSES.get(row['status'], row['status']),
                 '岗位链接：' + row['url'], '模式：不发送验证', '']
        lines += detail.get('pause_reasons', [])
        if detail.get('error'):
            lines.append(detail['error'])
        if detail.get('crosscheck'):
            lines += ['\n截图与文字交叉核验', json.dumps(detail['crosscheck'], ensure_ascii=False, indent=2)]
        research = detail.get('research', {})
        brief = research.get('brief') or {}
        if research:
            lines += ['\n公司研究', '研究时间：' + research.get('checked_at', ''),
                      '身份与来源状态：' + ('通过当前证据核验' if research.get('status') == 'verified_sources' else '待核实'),
                      brief.get('identity_summary', '')]
            review = research.get('claim_review') or {}
            if review:
                lines += ['身份复核理由：' + review.get('identity_reason', '')]
                if review.get('identity_quotes_verified'):
                    for evidence in review.get('identity_evidence', []):
                        lines += ['身份原文：' + evidence['evidence_quote'], '来源：' + evidence['source_url']]
            for claim in brief.get('claims', []):
                lines += [f'\n{claim["topic"]}：{claim["claim"]}', '原文：' + claim['evidence_quote'], '来源：' + claim['source_url']]
            lines += ['\n未确定事项 / 风险'] + brief.get('uncertainties', []) + brief.get('risks', [])
            lines += ['无法读取：' + page['url'] + ' · ' + page['error'] for page in research.get('failed', [])]
        assessment = detail.get('assessment')
        if assessment:
            lines += [f'\n岗位质量：{row["quality"]} / 100    个人匹配：{row["match"]} / 100', '岗位质量权重：成长 40% / 前景 30% / 背景 20% / 跳槽认可 10%']
            for key, label in [('skill_growth', '技能成长'), ('business_outlook', '业务前景'), ('company_background', '公司背景'), ('career_recognition', '未来跳槽认可度'), ('personal_match', '个人匹配')]:
                part = assessment[key]
                lines += [f'\n{label}：{part["score"]} / 100', part['reason']] + part['source_urls']
            lines += ['\n缺口'] + assessment['gaps'] + ['\n建议', assessment['recommendation']]
        lines += ['\n处理记录'] + [event['created_at'] + '  ' + event['detail'] for event in self.store.events(row['id'])]
        window = self.text_window('岗位研究与草稿结果', '\n'.join(lines))
        if detail.get('exports'):
            directory = self.private / 'jobs' / row['id']
            ttk.Button(window, text='打开本地草稿文件夹', command=lambda: os.startfile(directory)).pack(pady=8)

    def start_batch(self):
        self.start([row['id'] for row in reversed(self.store.jobs()) if row['status'] == 'queued'])

    def start_selected(self):
        self.start(list(self.jobs.selection()))

    def start(self, ids: list[str]):
        if self.busy or self.capturing:
            return
        if not ids:
            self.status.set('没有待处理岗位；请先加入岗位或选中一个暂停岗位。')
            return
        try:
            client = ModelClient.local(self.private, cancelled=self.cancelled.is_set)
        except Exception:
            self.status.set('无法读取已验证的 API 配置和加密 Key，请先通过 API 配置窗口完成检测并保存。')
            return
        self.cancelled.clear()
        self.busy = True
        self.screens_box.configure(state='disabled')
        self.capture_button.configure(state='disabled')
        self.batch_capture_button.configure(state='disabled')
        self.limit_box.configure(state='disabled')
        self.run_button.configure(state='disabled')
        self.selected_button.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        self.status.set(f'开始分析 {len(ids)} 个岗位；个别岗位异常会暂停并继续其他岗位。')
        def worker():
            pipeline = Pipeline(self.store, client, self.private, lambda job_id, message: self.events.put(('progress', message)))
            stopped = False
            try:
                for job_id in ids:
                    if self.cancelled.is_set():
                        break
                    pipeline.run(job_id)
            except Exception:
                stopped = True
                self.events.put(('progress', '批次已停止：鉴权、权限、额度或运行异常；未发送任何求职消息。'))
            finally:
                self.events.put(('done', stopped))
        threading.Thread(target=worker, daemon=True).start()

    def cancel(self):
        self.cancelled.set()
        self.cancel_button.configure(state='disabled')
        if self.delivery_panel.running:
            self.status.set('正在停止执行；已经发生的发送无法撤回，未确认结果将标记待核实。')
        else:
            self.status.set('正在停止浏览器采集；已完成项保留且不会自动分析。' if self.capturing else '已停止后续请求，正在等待已发出的请求返回或超时。')

    def drain(self):
        if self.closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.status.set(value)
                    self.log.configure(state='normal')
                    self.log.insert('end', value + '\n')
                    self.log.see('end')
                    self.log.configure(state='disabled')
                elif kind == 'delivery_done':
                    self.delivery_panel.finish(value)
                elif kind == 'capture_error':
                    self.finish_capture()
                    self.status.set(value)
                elif kind == 'capture_ready':
                    self.finish_capture()
                    try:
                        job_id, inserted = self.import_capture(Path(value))
                        self.refresh()
                        self.jobs.selection_set(job_id)
                        if inserted and not self.cancelled.is_set():
                            self.start([job_id])
                        elif inserted:
                            self.status.set('已保存岗位；分析已取消。')
                        else:
                            self.status.set('该岗位链接已经存在，没有重复导入或重复请求。')
                    except Exception:
                        self.status.set('浏览器内容未能加入队列，请重新导入。')
                elif kind == 'batch_capture_ready':
                    self.finish_capture()
                    ids, failed = [], 0
                    try:
                        manifest = Path(value)
                        report = json.loads(manifest.read_text(encoding='utf-8'))
                        duplicates = report.get('known_skipped', 0)
                        failed = len(report.get('failures', []))
                        for entry in report['captures']:
                            try:
                                path = Path(entry['manifest']).resolve()
                                if path.parent.parent != manifest.parent.resolve():
                                    raise ValueError('采集路径越界')
                                job_id, inserted = self.import_capture(path, expected_url=entry['url'])
                                if inserted:
                                    ids.append(job_id)
                                else:
                                    duplicates += 1
                            except Exception:
                                failed += 1
                        self.refresh()
                        if ids:
                            self.jobs.selection_set(ids)
                        message = f'自动采集：新增 {len(ids)}，已有 {duplicates}，失败 {failed}。'
                        if report['status'] != 'completed' or self.cancelled.is_set():
                            self.status.set(message + report.get('reason', '已停止。') + ' 已导入项留在待办，未开始分析。')
                        elif ids:
                            self.start(ids)
                            self.status.set(message + ' 正在自动进行不发送分析。')
                        else:
                            self.status.set(message + ' 没有新增岗位；未发起模型请求。')
                    except Exception:
                        self.status.set('批次清单读取失败；原始证据保留在本地，未开始分析。')
                elif kind == 'done':
                    self.busy = False
                    self.screens_box.configure(state='readonly')
                    self.capture_button.configure(state='normal')
                    self.batch_capture_button.configure(state='normal')
                    self.limit_box.configure(state='readonly')
                    self.run_button.configure(state='normal')
                    self.selected_button.configure(state='normal')
                    self.cancel_button.configure(state='disabled')
                    if not value:
                        self.status.set('本轮分析已结束。双击岗位查看核验、研究与草稿结果；没有发送或提交。')
                self.refresh()
        except queue.Empty:
            pass
        self.poll_id = self.root.after(100, self.drain)

    def open_setup(self):
        python = Path(sys.executable).with_name('pythonw.exe')
        subprocess.Popen([str(python if python.exists() else Path(sys.executable)), '-m', 'job_agent.setup_ui'], cwd=ROOT)

    def close(self):
        self.cancelled.set()
        self.closed = True
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


@contextmanager
def single_instance(private: Path):
    private.mkdir(parents=True, exist_ok=True)
    handle = (private / 'workbench.lock').open('a+b')
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        if (private / 'workbench.lock').stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise ValueError('工作台已经打开，请切换到现有窗口。') from None
    else:
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('工作台已经打开。') from None
    try:
        yield
    finally:
        handle.close()


def main():
    root = tk.Tk()
    root.withdraw()
    try:
        with single_instance(PRIVATE):
            Workbench(root)
            root.deiconify()
            root.mainloop()
    except ValueError as exc:
        messagebox.showinfo('本地求职助手', str(exc), parent=root)
        root.destroy()


if __name__ == '__main__':
    main()
