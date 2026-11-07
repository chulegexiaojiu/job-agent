"""Visible UI Automation adapters. No cookies, hidden APIs, injected JS or OCR clicks.

Production adapters remain opt-in until the site/profile is reviewed locally.
Unknown controls, job identity, redirect, or success evidence stop the action.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from job_agent.browser_batch import contained, RESTRICTED
from job_agent.browser_capture import _browser_process, rect
from job_agent.delivery import DeliveryBlocked, BatchStopped, classify_reply, digest, normalize, recent_reply_time
from job_agent.settings import atomic_write
from job_agent.store import utc_now


def exact_url(value):
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in {None,443}:
        raise DeliveryBlocked('执行页面必须是无账号密码的 HTTPS 地址。')
    return value.rstrip('#')


def text(node):
    children=[n.window_text().strip() for n in node.descendants() if n.element_info.control_type in {'Text','Hyperlink'} and n.window_text().strip()]
    return '\n'.join(children) if children else node.window_text().strip()


class VisibleBrowser:
    def __init__(self, url: str, *, allow_local_test=False):
        from pywinauto import Desktop
        self.desktop = Desktop
        self.allow_local_test=allow_local_test
        self.url = self.valid_url(url)
        found=[]
        for w in Desktop(backend='win32').windows(visible_only=True):
            if w.class_name() != 'Chrome_WidgetWin_1' or w.is_minimized() or not _browser_process(w.process_id()):
                continue
            for d in Desktop(backend='uia').window(handle=w.handle).descendants(control_type='Document'):
                try:
                    if str(d.iface_value.CurrentValue)==self.url and rect(d)[2]>rect(d)[0]:
                        found.append(w)
                except Exception:
                    continue
        if len(found)!=1:
            raise DeliveryBlocked('请只打开一个与计划链接完全相同的浏览器标签页。')
        self.window=found[0]
        self.guard()

    def valid_url(self,url):
        parts=urlsplit(url)
        if (self.allow_local_test and parts.scheme=='http' and parts.hostname=='127.0.0.1'
                and parts.port and not parts.username and not parts.password):return url
        return exact_url(url)

    def guard(self, *, allowed_url=None):
        if not self.window.is_visible() or self.window.is_minimized():
            raise BatchStopped('执行浏览器已关闭或最小化。')
        docs=[]
        for d in self.desktop(backend='uia').window(handle=self.window.handle).descendants(control_type='Document'):
            if rect(d)[2]>rect(d)[0] and rect(d)[3]>rect(d)[1]:
                docs.append(d)
        if len(docs)!=1:
            raise BatchStopped('当前页面无法唯一确认，停止批次。')
        d=docs[0]
        value=str(d.iface_value.CurrentValue)
        if value != (allowed_url or self.url):
            raise BatchStopped('执行期间标签页或地址发生变化。')
        for n in d.descendants(control_type='Text'):
            if contained(rect(n),rect(d)) and (RESTRICTED.search(n.window_text()) or re.search(r'今日.{0,12}(上限|用完)|额度不足|次数已用完',n.window_text())):
                raise BatchStopped('页面出现登录、验证、频率或额度限制；停止批次。')
        return d

    def navigate(self, target, cancelled=lambda:False):
        """Navigate the already bound window once, accepting only the exact next URL."""
        target=self.valid_url(target)
        if target==self.url:return self.guard()
        if cancelled():raise BatchStopped('执行已取消。')
        self.guard();previous=self.url
        edits=self.desktop(backend='uia').window(handle=self.window.handle).descendants(control_type='Edit')
        displays={previous,re.sub(r'^https?://','',previous)}
        bars=[n for n in edits if n.class_name()=='OmniboxViewViews' and n.is_visible()
              and str(n.iface_value.CurrentValue) in displays]
        if len(bars)!=1:raise BatchStopped('无法唯一确认浏览器地址栏。')
        self.window.set_focus();self.guard()
        bars[0].iface_value.SetValue(target)
        if str(bars[0].iface_value.CurrentValue)!=target:raise BatchStopped('地址栏读回不一致。')
        bars[0].type_keys('{ENTER}',set_foreground=True)
        deadline=time.monotonic()+15;ready=None
        while time.monotonic()<deadline:
            if cancelled():raise BatchStopped('执行已取消。')
            if not self.window.is_visible() or self.window.is_minimized():raise BatchStopped('执行浏览器已关闭或最小化。')
            docs=self.desktop(backend='uia').window(handle=self.window.handle).descendants(control_type='Document')
            docs=[d for d in docs if rect(d)[2]>rect(d)[0] and rect(d)[3]>rect(d)[1]]
            if len(docs)>1:raise BatchStopped('导航页面无法唯一确认。')
            if not docs:time.sleep(.1);continue
            value=str(docs[0].iface_value.CurrentValue)
            if value not in {target,previous}:raise BatchStopped('导航出现非计划地址或重定向。')
            if value==target:
                self.url=target;self.guard()
                if ready is None:ready=time.monotonic()
                if time.monotonic()-ready>=.5:return self.guard()
            else:ready=None
            time.sleep(.1)
        raise BatchStopped('计划页面加载超时，停止批次。')

    def nodes(self, selector, *, root=None, visible=True):
        d=self.guard()
        root=root or d
        if not selector or set(selector)-{'type','name','class','id'}:
            raise DeliveryBlocked('控件选择器无效。')
        result=[]
        for n in root.descendants():
            if selector.get('type') and n.element_info.control_type!=selector['type']:continue
            if selector.get('name') is not None and n.window_text().strip()!=selector['name']:continue
            if selector.get('class') is not None and n.class_name()!=selector['class']:continue
            if selector.get('id') is not None and n.element_info.automation_id!=selector['id']:continue
            if visible and (not n.is_visible() or not contained(rect(n),rect(d))):continue
            result.append(n)
        return result

    def one(self, selector, *, root=None):
        nodes=self.nodes(selector,root=root)
        if len(nodes)!=1:
            raise DeliveryBlocked('页面控件缺失、重复或不完整显示。')
        return nodes[0]

    def click(self, selector):
        n=self.one(selector)
        if not n.is_enabled() or 'disabled' in n.class_name().split():
            raise DeliveryBlocked('目标控件不可用。')
        self.window.set_focus()
        self.guard()
        # Invoke acts on the bound UIA element, never stale screen coordinates.
        n.iface_invoke.Invoke()

    def fill(self, selector, value):
        node=self.one(selector)
        self.window.set_focus()
        self.guard()
        from pywinauto.uia_defines import NoPatternInterfaceError
        try:
            pattern=node.iface_value
        except NoPatternInterfaceError:
            # Choose an available interface before writing; never retry a failed write.
            pattern=node.iface_legacy_iaccessible
        pattern.SetValue(value)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            self.guard()
            if str(pattern.CurrentValue)==value:
                time.sleep(.15)
                if str(pattern.CurrentValue)==value:return
            time.sleep(.05)
        raise DeliveryBlocked('输入字段读回不一致。')

    def upload(self, selector, attachment: Path):
        node=self.one(selector)
        if not node.is_enabled():raise DeliveryBlocked('附件选择控件不可用。')
        self.window.set_focus();self.guard()
        node.click_input()
        import ctypes
        from ctypes import wintypes
        user32=ctypes.WinDLL('user32',use_last_error=True)
        user32.GetWindow.argtypes=[wintypes.HWND,wintypes.UINT]
        user32.GetWindow.restype=wintypes.HWND
        def owned(window):
            handle=window.handle
            for _ in range(6):
                handle=user32.GetWindow(handle,4)
                if handle==self.window.handle:return True
                if not handle:break
            return False
        deadline=time.monotonic()+5
        dialogs=[]
        while time.monotonic()<deadline:
            dialogs=[w for w in self.desktop(backend='win32').windows(visible_only=True)
                     if w.class_name()=='#32770' and owned(w)]
            if dialogs:break
            time.sleep(.1)
        if len(dialogs)!=1:
            raise DeliveryBlocked('无法唯一定位本浏览器的文件选择窗口。')
        dlg=self.desktop(backend='uia').window(handle=dialogs[0].handle)
        edits=[n for n in dlg.descendants(control_type='Edit')
               if n.element_info.automation_id in {'1148','1001'} and n.is_visible()]
        opens=[n for n in dlg.descendants(control_type='Button')
               if n.element_info.automation_id=='1' and n.is_enabled()]
        if len(edits)!=1 or len(opens)!=1:
            raise DeliveryBlocked('文件选择器结构不受支持，未选择附件。')
        edits[0].iface_value.SetValue(str(attachment.resolve()))
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and str(edits[0].iface_value.CurrentValue)!=str(attachment.resolve()):
            time.sleep(.05)
        if str(edits[0].iface_value.CurrentValue)!=str(attachment.resolve()):
            raise DeliveryBlocked('文件名读回不一致。')
        opens[0].iface_invoke.Invoke()
        time.sleep(.4)
        self.guard()

    def evidence(self, directory: Path, extra: dict):
        d=self.guard()
        directory.mkdir(parents=True,exist_ok=True)
        image=directory/'result.png'
        # Bound document capture only; no other browser tabs or desktop content.
        self.window.set_focus()
        bounds=rect(d)
        from job_agent.browser_capture import visible_region_capture
        picture=visible_region_capture(self.window,rect(self.window),[bounds])
        # visible_region_capture returns the window-sized image; crop to document.
        wb=rect(self.window)
        picture.crop((bounds[0]-wb[0],bounds[1]-wb[1],bounds[2]-wb[0],bounds[3]-wb[1])).save(image)
        result={'captured_at':utc_now(),'page_url':self.url,'screenshot':str(image.resolve()),
                'screenshot_sha256':digest(image),**extra}
        atomic_write(directory/'receipt.json',json.dumps(result,ensure_ascii=False,indent=2).encode())
        return result


class PlannedBrowser:
    """Reuse one exact visible browser for an explicitly enabled live batch."""
    def __init__(self,cancelled=lambda:False):self.browser=None;self.cancelled=cancelled
    def __call__(self,url):
        if self.browser is None:self.browser=VisibleBrowser(url)
        else:self.browser.navigate(url,self.cancelled)
        return self.browser


class BossAdapter:
    adapter_id='boss-uia-v1'
    start_selector={'type':'Hyperlink','class':'btn btn-startchat','name':'立即沟通'}
    input_selector={'id':'chat-input','class':'chat-input'}
    send_selector={'type':'Button','name':'发送'}

    def __init__(self, private: Path, browser_factory=VisibleBrowser):
        self.private,self.browser_factory=private,browser_factory
        self.browser=None

    def bind(self,action):
        self.browser=self.browser_factory(action['target_url'])
        return self.browser

    def identity(self,action,chat=False):
        b=self.browser;d=b.guard();p=action['payload']
        if chat:
            # A generic chat URL is not a recipient identity. Require a job-card link.
            matches=[]
            for n in d.descendants(control_type='Hyperlink'):
                try:
                    from job_agent.browser_capture import detail_url
                    if detail_url(str(n.iface_value.CurrentValue))==p['job_url']:
                        matches.append(n)
                except Exception:
                    continue
            if len(matches)!=1:
                raise DeliveryBlocked('会话没有唯一对应岗位卡片，不能确认收件人。')
            card=text(matches[0].parent())
            lines={normalize(line) for line in card.splitlines()}
            if normalize(p['title']) not in lines or normalize(p['company']) not in lines:
                raise DeliveryBlocked('会话岗位卡片与公司、职位不一致。')
        else:
            header=b.one({'class':'info-primary'})
            from job_agent.browser_capture import text_between
            anchors=[n for n in d.descendants(control_type='Text') if n.window_text()=='公司基本信息']
            ends=[n for n in d.descendants(control_type='Hyperlink') if n.window_text().strip()=='查看全部职位']
            if len(anchors)!=1 or len(ends)!=1:
                raise DeliveryBlocked('公司区域无法唯一定位。')
            company=text_between(d,anchors[0],ends[0])
            if normalize(p['title']) not in {normalize(line) for line in text(header).splitlines()} or normalize(p['company']) not in {normalize(line) for line in company.splitlines()}:
                raise DeliveryBlocked('详情页公司或岗位与计划不一致。')

    def messages(self, mine=True):
        cls='message-item item-myself' if mine else 'message-item item-friend'
        return self.browser.nodes({'type':'ListItem','class':cls},visible=False)

    def latest_reply(self):
        replies=self.messages(mine=False)
        if not replies:raise DeliveryBlocked('没有可读取的招聘方回复。')
        item=replies[-1]
        timestamps={text(n) for n in item.descendants()
                    if set(n.class_name().split()) & {'message-time','message-timestamp'}}
        stamp=next(iter(timestamps)) if len(timestamps)==1 else ''
        body='\n'.join(line for line in text(item).splitlines() if line not in timestamps)
        return body,stamp

    def check_reply(self,action):
        body,stamp=self.latest_reply()
        recent_reply_time(stamp)
        if (classify_reply(body)!='resume_requested' or body!=action['payload'].get('reply_text')
                or stamp!=action['payload'].get('reply_sent_at')):
            raise DeliveryBlocked('当前会话最新回复或消息时间与计划中的明确简历请求不一致。')
        return body,stamp

    def inspect(self,action):
        if action['kind'] not in {'boss_greeting','resume_send'}:
            raise DeliveryBlocked('BOSS 适配器不处理外链表单。')
        self.validate_target(action['target_url'])
        self.bind(action)
        self.identity(action,chat=action['kind']=='resume_send')
        if action['kind']=='boss_greeting':
            self.browser.one(self.start_selector)
        else:
            self.browser.one({'type':'Button','name':'选择文件: 未选择文件'})
            self.browser.one(self.send_selector)
            self.check_reply(action)
        return {'adapter':self.adapter_id,'target_matches':True,'read_only':True,'kind':action['kind']}

    def validate_target(self,value):
        exact_url(value)
        if urlsplit(value).hostname not in {'www.zhipin.com','zhipin.com'}:
            raise DeliveryBlocked('BOSS 执行目标域名不符。')

    def chat_destination(self,value):
        parts=urlsplit(value)
        return (parts.scheme=='https' and parts.hostname=='www.zhipin.com' and parts.path=='/web/geek/chat'
                and not parts.username and not parts.password and parts.port in {None,443})

    def execute(self,action,attachment,checkpoint):
        b=self.browser
        self.identity(action,chat=action['kind']=='resume_send')
        checkpoint()
        if action['kind']=='boss_greeting':
            # The platform may send its own greeting on this click: this is already external I/O.
            b.click(self.start_selector)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                docs=b.desktop(backend='uia').window(handle=b.window.handle).descendants(control_type='Document')
                values=[]
                for d in docs:
                    try:
                        v=str(d.iface_value.CurrentValue)
                        if rect(d)[2]>rect(d)[0]:values.append(v)
                    except Exception:pass
                if len(values)==1 and self.chat_destination(values[0]):
                    b.url=values[0];break
                time.sleep(.2)
            self.identity(action,chat=True)
            # Existing/default outgoing content is ambiguous. Do not send a second greeting.
            if self.messages(mine=True):
                raise DeliveryBlocked('开聊后发现已有发出消息；需核实是否平台已自动问候，未重复发送。')
            checkpoint()
            b.fill(self.input_selector,action['payload']['greeting'])
        else:
            self.check_reply(action)
            if self.messages(mine=True):
                for item in self.messages(mine=True):
                    if attachment.name in text(item):
                        raise DeliveryBlocked('会话中已出现该附件，不重复发送。')
            checkpoint()
            b.upload({'type':'Button','name':'选择文件: 未选择文件'},attachment)
            # Require the selected full unique filename in the attachment preview.
            if attachment.name not in text(b.guard()):
                raise DeliveryBlocked('未找到该岗位的唯一附件预览名。')
        self.identity(action,chat=True)
        checkpoint()
        before=len(self.messages(mine=True))
        if action['kind']=='resume_send':
            self.check_reply(action)
        b.click(self.send_selector)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            self.identity(action,chat=True)
            outgoing=self.messages(mine=True)
            if len(outgoing)>before:
                item=outgoing[-1]
                value=text(item)
                expected=action['payload']['greeting'] if action['kind']=='boss_greeting' else attachment.name
                statuses=[n.class_name() for n in item.descendants()]
                accepted=any(c in {'message-status status-delivery','message-status status-read'} for c in statuses)
                if expected in value and accepted:
                    return b.evidence(self.private/'outbound'/action['id'],{'job_url':action['payload']['job_url'],
                        'kind':action['kind'],'message':value,'delivery_status':'accepted',
                        'attachment_sha256':digest(attachment) if action['kind']=='resume_send' else '',
                        'new_message_count':len(outgoing)-before})
            time.sleep(.3)
        raise DeliveryBlocked('未读取到本次发出消息的送达状态。')

    def verify(self,action,evidence,attachment):
        expected=action['payload']['greeting'] if action['kind']=='boss_greeting' else attachment.name
        return (evidence.get('job_url')==action['payload']['job_url'] and evidence.get('kind')==action['kind']
                and expected in evidence.get('message','') and evidence.get('delivery_status')=='accepted'
                and evidence.get('new_message_count',0)>0
                and (action['kind']!='resume_send' or evidence.get('attachment_sha256')==digest(attachment)))

    def capture_reply(self,action):
        self.validate_target(action['target_url'])
        self.bind(action); self.identity(action,chat=True)
        latest,stamp=self.latest_reply()
        from uuid import uuid4
        evidence=self.browser.evidence(self.private/'replies'/action['job_id']/uuid4().hex,
            {'job_url':action['payload']['job_url'],'text':latest,'classification':classify_reply(latest),
             'message_sent_at':stamp})
        self.identity(action,chat=True)
        if self.latest_reply()!=(latest,stamp):raise DeliveryBlocked('采集期间招聘方回复已变化，请重新读取。')
        return latest,Path(evidence['screenshot']).with_name('receipt.json')


class FormAdapter:
    """Exact one-page form profile; only identity and confirmed availability fields.

    Profiles bind a complete URL + company + job text + each visible input and a
    receipt locator. Unknown fields/checkboxes, multi-step forms, consent/signing,
    or credentials are never answered automatically.
    """
    allowed_fields={'name','email','phone','arrival_date','days_per_week','duration_months'}

    def __init__(self, private, profile, identity, browser_factory=VisibleBrowser):
        self.private,self.profile,self.identity_data=private,profile,identity
        self.browser_factory=browser_factory
        self.adapter_id='form:'+profile['id']

    def identity(self,action):
        p=self.profile
        if action['target_url']!=p['url'] or action['payload']['company']!=p['company'] or action['payload']['title']!=p['title']:
            raise DeliveryBlocked('网申配置与目标岗位不匹配。')
        b=self.browser
        company=text(b.one(p['company_selector']))
        title=text(b.one(p['title_selector']))
        if normalize(company)!=normalize(p['company']) or normalize(title)!=normalize(p['title']):
            raise DeliveryBlocked('网申页面公司或岗位标题不一致。')
        return b.guard()

    def values(self):
        values={k:self.identity_data.get(k,'') for k in ('name','phone','email')}
        values.update(arrival_date=(date.today()+timedelta(days=14)).isoformat(),days_per_week='5',duration_months='6')
        return values

    def inspect(self,action):
        if action['kind']!='external_submit':raise DeliveryBlocked('该适配器仅处理网申。')
        self.browser=self.browser_factory(action['target_url'])
        p=self.profile
        if p.get('reviewed') is not True or not p.get('identity_source_url') or not p.get('identity_evidence_quote'):
            raise DeliveryBlocked('网申域名与招聘主体关联尚未登记审核。')
        if set(p['fields'])-self.allowed_fields:
            raise DeliveryBlocked('存在未确认答案的表单字段。')
        d=self.identity(action)
        values=self.values()
        expected=[]
        for key,selector in p['fields'].items():
            if not values[key]:raise DeliveryBlocked('个人资料缺少该表单必填值。')
            expected.append(self.browser.one(selector))
        edits=d.descendants(control_type='Edit')
        if len(edits)!=len(expected) or {tuple(n.element_info.runtime_id) for n in edits}!={tuple(n.element_info.runtime_id) for n in expected}:
            raise DeliveryBlocked('页面存在未映射的输入框，需核实其用途。')
        if any(d.descendants(control_type=t) for t in ('CheckBox','ComboBox','RadioButton')):
            raise DeliveryBlocked('表单含选项、声明或同意项；当前适配器不自动作答。')
        from job_agent.delivery import SENSITIVE
        if SENSITIVE.search(text(d)):
            raise DeliveryBlocked('表单含敏感资料、付款或签署要求。')
        self.browser.one(p['upload_selector']);self.browser.one(p['submit_selector'])
        if self.browser.nodes(p['receipt_selector']):
            raise DeliveryBlocked('页面已经存在提交凭据，不能判断为本次提交。')
        return {'adapter':self.adapter_id,'target_matches':True,'mapped_fields':list(p['fields']),'read_only':True}

    def execute(self,action,attachment,checkpoint):
        b=self.browser;p=self.profile
        self.identity(action);checkpoint()
        for key,selector in p['fields'].items():
            checkpoint();self.identity(action)
            b.fill(selector,self.values()[key])
        checkpoint();self.identity(action)
        b.upload(p['upload_selector'],attachment)
        preview=text(b.one(p['attachment_selector']))
        if normalize(attachment.name)!=normalize(preview):raise DeliveryBlocked('网申附件预览与岗位专属文件名不一致。')
        checkpoint();self.identity(action)
        # Verify filled values again immediately before submission.
        for key,selector in p['fields'].items():
            if str(b.one(selector).iface_value.CurrentValue)!=self.values()[key]:
                raise DeliveryBlocked('提交前字段读回不一致。')
        # A new declaration or field appearing after upload is not implicitly answered.
        d=b.guard()
        expected=[b.one(s) for s in p['fields'].values()]
        edits=d.descendants(control_type='Edit')
        if {tuple(n.element_info.runtime_id) for n in edits}!={tuple(n.element_info.runtime_id) for n in expected}:
            raise DeliveryBlocked('提交前表单字段发生变化。')
        if any(d.descendants(control_type=t) for t in ('CheckBox','ComboBox','RadioButton')):
            raise DeliveryBlocked('提交前出现声明或选项。')
        from job_agent.delivery import SENSITIVE
        if SENSITIVE.search(text(d)):raise DeliveryBlocked('提交前出现敏感要求。')
        if b.nodes(p['receipt_selector']):raise DeliveryBlocked('提交前已出现回执，停止重复提交。')
        b.click(p['submit_selector'])
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            # v1 intentionally supports same-page receipts only; redirects are unknown.
            d=b.guard();nodes=b.nodes(p['receipt_selector'])
            if len(nodes)==1:
                receipt=text(nodes[0])
                match=re.fullmatch(re.escape(p['receipt_prefix'])+r'(?P<receipt_id>[A-Za-z0-9-]{4,80})',receipt)
                if match and match.groupdict().get('receipt_id'):
                    return b.evidence(self.private/'outbound'/action['id'],{'kind':action['kind'],
                        'job_url':action['payload']['job_url'],'receipt_text':receipt,
                        'receipt_id':match.group('receipt_id'),'attachment_sha256':digest(attachment),
                        'attachment_name':attachment.name,'company':p['company'],'title':p['title']})
            time.sleep(.3)
        raise DeliveryBlocked('未读取到带申请编号的本次提交回执。')

    def verify(self,action,evidence,attachment):
        return (evidence.get('job_url')==action['payload']['job_url'] and evidence.get('kind')=='external_submit'
            and evidence.get('company')==action['payload']['company'] and evidence.get('title')==action['payload']['title']
            and bool(evidence.get('receipt_id')) and evidence.get('attachment_sha256')==digest(attachment)
            and evidence.get('attachment_name')==attachment.name)
