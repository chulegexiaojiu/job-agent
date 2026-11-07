"""Bounded list scanning tests; scroll/next-page UI is synthetic."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from job_agent.browser_batch import BrowserSession,PageLoading
from job_agent.browser_capture import CaptureError,CaptureStopped

BASE='https://www.zhipin.com/web/geek/jobs'

def link(name):
    n=Mock();n.class_name.return_value='job-name';n.window_text.return_value=name
    n.iface_value.CurrentValue=f'https://www.zhipin.com/job_detail/{name}.html'
    n.element_info.control_type='Hyperlink';return n


def session(pages,*,next_page=False,changed_filter=False,empty_during_navigation=False):
    s=BrowserSession.__new__(BrowserSession);s.origin=s.expected=BASE;s.origin_value=BASE+'?query=AI&page=1';s.window=Mock()
    state={'page':0,'loads':0};documents=[]
    for index,names in enumerate(pages):
        d=Mock();rows=[link(n) for n in names]
        d.iface_scroll.CurrentVerticallyScrollable=not next_page
        d.iface_scroll.CurrentVerticalScrollPercent=0 if index<len(pages)-1 else 100
        button=Mock();button.window_text.return_value='下一页';button.element_info.control_type='Button';button.class_name.return_value='next'
        def advance(index=index):state['page']=min(index+1,len(pages)-1);state['loads']=int(empty_during_navigation)
        button.iface_invoke.Invoke.side_effect=advance;d.scroll.side_effect=lambda *a,_advance=advance,**kw:_advance()
        def descendants(control_type=None,rows=rows,button=button,index=index):
            if control_type=='Hyperlink':return rows
            if control_type=='Text':return []
            return rows+([button] if next_page and index<len(pages)-1 else [])
        d.descendants.side_effect=descendants;documents.append(d)
    def current():
        if state['loads']:
            state['loads']-=1;raise PageLoading('loading')
        query='other' if changed_filter and state['page']>0 else 'AI'
        page=state['page']+1 if next_page else 1
        return documents[state['page']],BASE+f'?query={query}&page={page}'
    s.current=current
    return s,documents


class PaginationTests(unittest.TestCase):
    def test_scroll_skips_known_and_deduplicates_across_screens(self):
        s,docs=session([['old'],['old','a'],['a','b']])
        with patch('job_agent.browser_batch.rect',return_value=(0,0,100,100)),patch('job_agent.browser_batch.time.sleep'):
            rows,old=s.scan_candidates({'https://www.zhipin.com/job_detail/old.html'},2,3)
        self.assertEqual([r['label'] for r in rows],['a','b']);self.assertEqual(old,1)
        self.assertEqual(len(s.scan_steps),3);docs[0].scroll.assert_called_once();docs[1].scroll.assert_called_once()

    def test_next_page_allows_only_page_number_and_handles_empty_tree(self):
        s,docs=session([['old'],['new']],next_page=True,empty_during_navigation=True)
        with patch('job_agent.browser_batch.rect',return_value=(0,0,100,100)),patch('job_agent.browser_batch.time.sleep'):
            rows,old=s.scan_candidates({'https://www.zhipin.com/job_detail/old.html'},1,2)
        self.assertEqual(rows[0]['label'],'new');self.assertTrue(s.origin_value.endswith('page=2'))

    def test_changed_filters_stop_and_do_not_collect_next_page(self):
        s,_=session([['old'],['new']],next_page=True,changed_filter=True)
        with patch('job_agent.browser_batch.rect',return_value=(0,0,100,100)),patch('job_agent.browser_batch.time.sleep'):
            with self.assertRaises(CaptureStopped):s.scan_candidates({'https://www.zhipin.com/job_detail/old.html'},1,2)

    def test_bounds_and_no_progress(self):
        s,docs=session([['old'],['old'],['new']])
        for value in [True,0,21]:
            with self.assertRaises(CaptureError):s.scan_candidates(set(),1,value)
        for value in [True,0,6]:
            with self.assertRaises(CaptureError):s.scan_candidates(set(),value,2)
        with patch('job_agent.browser_batch.rect',return_value=(0,0,100,100)),patch('job_agent.browser_batch.time.sleep'):
            rows,old=s.scan_candidates({'https://www.zhipin.com/job_detail/old.html'},1,5)
        self.assertEqual(rows,[]);self.assertEqual(s.scan_steps[-1]['stop'],'no_progress')
        docs[1].scroll.assert_not_called()

    def test_current_screen_default_does_not_scroll(self):
        s,docs=session([['first'],['second']]);s.candidates=Mock(return_value=([],0))
        self.assertEqual(s.scan_candidates(set(),1,1),([],0));s.candidates.assert_called_once();docs[0].scroll.assert_not_called()

if __name__=='__main__':unittest.main()
