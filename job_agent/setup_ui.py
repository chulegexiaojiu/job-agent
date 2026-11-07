'''Local API setup window. No recruitment automation is run.'''
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from job_agent.settings import (Connection, PRIVATE, SecretError, atomic_write, discover_connection,
                                forget_key, load_connection, load_key, save_connection, save_key)
from tools.check_api import Probe, Result

LABELS = {'text': '文本生成', 'vision': '截图识别（含中文岗位）',
          'structured_output': 'JSON 结构化输出', 'tool_call': '工具调用格式'}
STATUS = {'pass': '通过', 'fail': '未通过', 'error': '出错'}


def result_message(result: Result) -> str:
    if result.status == 'pass':
        return f'样本验证通过 · {result.seconds:.1f} 秒'
    if result.http_status:
        hints = {400: '请求被拒绝，请检查协议或长度参数',
                 401: 'Key 鉴权失败，请检查是否来自当前服务商',
                 403: '无权限使用接口或模型', 404: '接口路径或模型不存在',
                 405: '接口不支持此请求方式', 413: '接口拒绝图片大小',
                 422: '请求格式不受支持', 429: '额度不足或限流，已停止'}
        return f'HTTP {result.http_status}：' + hints.get(result.http_status, '接口错误')
    if result.status == 'fail':
        return '返回内容与测试值不符；不能认定该项可用'
    if 'timed out' in result.detail:
        return '连接失败或超时，请检查网络和地址'
    if 'Pillow' in result.detail:
        return '缺少 Pillow 图片组件'
    if 'Redirect' in result.detail:
        return '接口要求重定向，请填写最终地址'
    if 'truncated' in result.detail or 'completed' in result.detail:
        return '输出为空或未完成，可在高级配置提高输出长度'
    return '返回格式不兼容或检测未完成，请查看报告'


class SetupApp:
    def __init__(self, root: tk.Tk, *, private_dir: Path = PRIVATE,
                 initial_connection: Connection | None = None, probe_factory=Probe):
        self.root, self.private, self.probe_factory = root, private_dir, probe_factory
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.busy = self.closed = False
        self.connection = initial_connection
        error = ''
        if self.connection is None:
            try:
                self.connection = load_connection(self.private / 'connection.json') or discover_connection()
            except ValueError as exc:
                error = str(exc)
        self.connection = self.connection or Connection('', '')
        root.title('求职助手 · API 配置与检测')
        root.geometry('820x620')
        root.minsize(720, 600)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Body.TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('Treeview', rowheight=30, font=('Microsoft YaHei UI', 10))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text='粘贴 API Key，按回车即可检测', style='Title.TLabel').grid(
            row=0, column=0, sticky='w', pady=(0, 12))
        self.connection_text = tk.StringVar()
        ttk.Label(frame, textvariable=self.connection_text, style='Body.TLabel', wraplength=740).grid(
            row=1, column=0, sticky='w', pady=(0, 12))
        self.update_connection_label()
        self.key_text = tk.StringVar()
        self.key_entry = ttk.Entry(frame, textvariable=self.key_text, show='●', font=('Segoe UI', 13))
        self.key_entry.grid(row=2, column=0, sticky='ew', ipady=7)
        self.key_entry.bind('<Return>', self.start)
        self.remember = tk.BooleanVar(value=os.name == 'nt')
        self.remember_box = ttk.Checkbutton(frame, text='鉴权通过后加密保存在本机，供后续使用', variable=self.remember)
        self.remember_box.grid(row=3, column=0, sticky='w', pady=(8, 10))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky='ew')
        self.start_button = ttk.Button(buttons, text='开始检测（Enter）', command=self.start)
        self.start_button.pack(side='left')
        self.cancel_button = ttk.Button(buttons, text='停止后续检测', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left', padx=8)
        self.advanced_button = ttk.Button(buttons, text='高级配置', command=self.toggle_advanced)
        self.advanced_button.pack(side='left')
        self.forget_button = ttk.Button(buttons, text='清除已存 Key', command=self.forget)
        self.forget_button.pack(side='right')
        self.advanced = ttk.LabelFrame(frame, text='更换服务商时修改；Key 只发送到这里显示的接口', padding=10)
        self.advanced.columnconfigure(1, weight=1)
        self.variables, self.advanced_widgets = {}, []
        fields = [('base_url', '接口地址'), ('model', '模型名称'), ('protocol', '协议'),
                  ('chat_token_field', 'Chat 长度参数'), ('timeout', '单次超时（秒）'),
                  ('max_output_tokens', '最大输出 Token')]
        for index, (name, label) in enumerate(fields):
            ttk.Label(self.advanced, text=label).grid(row=index, column=0, sticky='w', padx=(0, 10), pady=3)
            variable = tk.StringVar(value=str(getattr(self.connection, name)))
            self.variables[name] = variable
            if name in {'protocol', 'chat_token_field'}:
                values = ['auto', 'chat', 'responses'] if name == 'protocol' else ['max_completion_tokens', 'max_tokens']
                widget = ttk.Combobox(self.advanced, textvariable=variable, values=values, state='readonly')
            else:
                widget = ttk.Entry(self.advanced, textvariable=variable)
            widget.grid(row=index, column=1, sticky='ew', pady=3)
            self.advanced_widgets.append(widget)
        ttk.Label(frame, text='仅发送合成文字和随机图片；不读取简历、不操作招聘网站。',
                  style='Body.TLabel').grid(row=6, column=0, sticky='w', pady=(16, 10))
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.grid(row=7, column=0, sticky='ew', pady=(0, 10))
        self.table = ttk.Treeview(frame, columns=('capability', 'status', 'detail'), show='headings', height=4)
        for name, text, width in [('capability', '检测项目', 180), ('status', '状态', 65), ('detail', '结果', 465)]:
            self.table.heading(name, text=text)
            self.table.column(name, width=width, minwidth=50, stretch=name == 'detail')
        for name, text in LABELS.items():
            self.table.insert('', 'end', iid=name, values=(text, '待检测', ''))
        self.table.grid(row=8, column=0, sticky='ew')
        self.status_text = tk.StringVar(value=error or '已准备好。粘贴当前接口的 API Key，然后按回车。')
        ttk.Label(frame, textvariable=self.status_text, style='Body.TLabel', wraplength=740).grid(
            row=9, column=0, sticky='w', pady=(12, 8))
        self.report_button = ttk.Button(frame, text='打开本地检测报告', command=self.open_report)
        self.report_button.grid(row=10, column=0, sticky='w')
        if not (self.private / 'api-capabilities.json').exists():
            self.report_button.configure(state='disabled')
        if not self.connection.base_url or not self.connection.model:
            self.toggle_advanced()
            self.status_text.set('未找到唯一的接口配置。请先填写地址和模型，再粘贴 Key。')
        elif (self.private / 'credential.dpapi').exists() and not error:
            self.status_text.set('已有本地加密凭据。直接按回车可复测；粘贴新 Key 可替换。')
        self.poll_id = root.after(100, self.drain_events)
        self.focus_id = root.after(150, self.key_entry.focus_set)

    def update_connection_label(self):
        base = self.connection.base_url or '待配置'
        model = self.connection.model or '待配置'
        self.connection_text.set(f'接口：{base}' + chr(10) + f'模型：{model}')

    def toggle_advanced(self):
        if self.busy:
            return
        if self.advanced.winfo_manager():
            self.advanced.grid_remove()
            self.root.geometry('820x620')
        else:
            self.advanced.grid(row=5, column=0, sticky='ew', pady=(12, 0))
            self.root.geometry('820x860')

    def get_connection(self) -> Connection:
        values = {name: value.get().strip() for name, value in self.variables.items()}
        try:
            values['timeout'] = float(values['timeout'])
            values['max_output_tokens'] = int(values['max_output_tokens'])
        except ValueError:
            raise ValueError('超时和输出长度必须填写数字。') from None
        return Connection(**values).validated()

    def set_busy(self, busy: bool):
        self.busy = busy
        for widget in [self.start_button, self.advanced_button, self.forget_button, self.remember_box, self.key_entry]:
            widget.configure(state='disabled' if busy else 'normal')
        for widget in self.advanced_widgets:
            widget.configure(state='disabled' if busy else ('readonly' if isinstance(widget, ttk.Combobox) else 'normal'))
        self.cancel_button.configure(state='normal' if busy else 'disabled')
        if busy:
            self.progress.start(15)
        else:
            self.progress.stop()

    def start(self, event=None):
        if self.busy or self.closed:
            return 'break'
        try:
            connection = self.get_connection()
            key = self.key_text.get().strip()
            if key.lower().startswith('bearer '):
                key = key[7:].strip()
            key = key or load_key(connection.base_url, self.private / 'credential.dpapi')
            if not key:
                raise ValueError('请粘贴当前接口服务商的 API Key。')
            if len(key) > 16384 or any(ord(c) < 32 for c in key):
                raise ValueError('Key 含有无效字符，请只粘贴密钥。')
            save_connection(connection, self.private / 'connection.json')
        except (ValueError, SecretError) as exc:
            self.status_text.set(str(exc))
            return 'break'
        except OSError:
            self.status_text.set('无法保存本地配置，请检查目录权限。')
            return 'break'
        self.connection = connection
        self.update_connection_label()
        self.key_text.set('')
        self.cancelled.clear()
        self.set_busy(True)
        self.report_button.configure(state='disabled')
        for name, text in LABELS.items():
            self.table.item(name, values=(text, '待检测', ''))
        self.table.item('text', values=(LABELS['text'], '检测中', '正在连接接口'))
        self.status_text.set('检测已开始，最多 5 次请求；鉴权失败或限流后停止。')
        threading.Thread(target=self.worker, args=(connection, key, self.remember.get()), daemon=True).start()
        return 'break'

    def worker(self, connection: Connection, key: str, remember: bool):
        try:
            probe = self.probe_factory(connection.base_url, connection.model, key, connection.timeout,
                                       connection.max_output_tokens, connection.chat_token_field,
                                       on_result=lambda result: self.events.put(('result', result)),
                                       cancelled=self.cancelled.is_set)
            report = probe.run(connection.protocol)
            saved, save_error = False, ''
            authenticated = any(r['capability'] == 'text' and r['status'] == 'pass' for r in report['results'])
            if not self.cancelled.is_set() and authenticated:
                try:
                    if remember:
                        save_key(key, connection.base_url, self.private / 'credential.dpapi')
                        saved = True
                    else:
                        forget_key(self.private / 'credential.dpapi')
                except (SecretError, OSError, ValueError):
                    save_error = '检测已完成，但 Key 保存失败；下次需要重新粘贴。'
            report['credential_saved_this_run'] = saved
            serialized = json.dumps(report, ensure_ascii=False, indent=2)
            serialized = serialized.replace(json.dumps(key, ensure_ascii=False)[1:-1], '[REDACTED]')
            atomic_write(self.private / 'api-capabilities.json', serialized.encode('utf-8'))
            self.events.put(('done', (report, save_error)))
        except Exception:
            self.events.put(('error', '检测未完成，请检查本地组件及目录权限；未标记为通过。'))
        finally:
            key = ''

    def drain_events(self):
        if self.closed:
            return
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == 'result':
                    self.table.item(data.capability, values=(LABELS[data.capability], STATUS[data.status], result_message(data)))
                    self.status_text.set(f'{LABELS[data.capability]}：{result_message(data)}')
                elif kind == 'done':
                    report, save_error = data
                    self.set_busy(False)
                    checked = {r['capability'] for r in report['results']}
                    for name in LABELS.keys() - checked:
                        self.table.item(name, values=(LABELS[name], '未执行', '前置检测失败或已停止'))
                    if report.get('cancelled'):
                        message = '检测已停止，未开始的请求没有发送。'
                    elif report['all_four_checks_passed']:
                        message = '四项样本通过，API 配置已就绪。求职投递功能尚未完成。'
                    elif report['text_and_vision_passed']:
                        message = '文本和图片样本通过，其他能力未全部通过，请查看结果。'
                    else:
                        message = 'API 尚未通过完整检测，请查看失败项目。'
                    if report['credential_saved_this_run']:
                        message += ' Key 已加密保存在本机。'
                    self.status_text.set(save_error or message)
                    self.report_button.configure(state='normal')
                    self.key_entry.focus_set()
                elif kind == 'error':
                    self.set_busy(False)
                    self.status_text.set(data)
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_id = self.root.after(100, self.drain_events)

    def cancel(self):
        self.cancelled.set()
        self.status_text.set('停止后续请求；已发送的请求会等待返回或超时。')
        self.cancel_button.configure(state='disabled')

    def forget(self):
        try:
            forget_key(self.private / 'credential.dpapi')
            self.key_text.set('')
            self.status_text.set('本地保存的 Key 已删除，接口地址和模型保留。')
        except OSError:
            self.status_text.set('无法删除本地凭据，请检查目录权限。')

    def open_report(self):
        path = self.private / 'api-capabilities.json'
        if path.exists():
            webbrowser.open(path.resolve().as_uri())

    def close(self):
        self.cancelled.set()
        self.closed = True
        for callback_id in (self.poll_id, self.focus_id):
            try:
                self.root.after_cancel(callback_id)
            except tk.TclError:
                pass
        self.key_text.set('')
        self.root.destroy()


def main():
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
