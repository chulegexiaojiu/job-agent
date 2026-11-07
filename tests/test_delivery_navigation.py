"""Deterministic browser navigation and exact form-source boundaries; no network."""
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock,patch

from job_agent.browser_delivery import VisibleBrowser,PlannedBrowser
from job_agent.delivery import BatchStopped,DeliveryBlocked
from job_agent.delivery_worker import adapter_for
from test_delivery import profile_fixture

class NavigationTests(unittest.TestCase):
    def browser(self, destination):
        browser=VisibleBrowser.__new__(VisibleBrowser)
        browser.url='https://example.test/first';browser.allow_local_test=False
        browser.window=Mock(handle=1);browser.window.is_visible.return_value=True;browser.window.is_minimized.return_value=False
        browser.guard=Mock()
        bar=Mock();bar.class_name.return_value='OmniboxViewViews';bar.is_visible.return_value=True
        bar.iface_value.CurrentValue='example.test/first'
        bar.iface_value.SetValue.side_effect=lambda value:setattr(bar.iface_value,'CurrentValue',value)
        secondary=Mock();secondary.class_name.return_value='OmniboxViewViews';secondary.is_visible.return_value=True;secondary.iface_value.CurrentValue=''
        doc=Mock();doc.iface_value.CurrentValue=destination
        window=Mock();window.descendants.side_effect=lambda control_type:[bar,secondary] if control_type=='Edit' else [doc]
        browser.desktop=Mock();browser.desktop.return_value.window.return_value=window
        return browser,bar

    @patch('job_agent.browser_delivery.time.sleep')
    @patch('job_agent.browser_delivery.rect',return_value=(0,0,100,100))
    def test_bound_navigation_filters_blank_secondary_bar_and_writes_once(self,*mocks):
        target='https://example.test/next?q=1';browser,bar=self.browser(target)
        with patch('job_agent.browser_delivery.time.monotonic',side_effect=[0,.1,.2,1,1.1]):browser.navigate(target)
        self.assertEqual(browser.url,target);bar.iface_value.SetValue.assert_called_once_with(target)
        bar.type_keys.assert_called_once_with('{ENTER}',set_foreground=True)

    @patch('job_agent.browser_delivery.rect',return_value=(0,0,100,100))
    def test_unexpected_redirect_stops_without_another_navigation(self,*mocks):
        browser,bar=self.browser('https://unplanned.test/redirect')
        with self.assertRaises(BatchStopped):browser.navigate('https://example.test/next')
        self.assertEqual(bar.iface_value.SetValue.call_count,1)

    def test_cancel_invalid_url_and_page_drift_never_write(self):
        for mode in ('cancel','url','drift'):
            browser,bar=self.browser('https://example.test/next')
            if mode=='drift':browser.guard.side_effect=BatchStopped('drift')
            with self.assertRaises(DeliveryBlocked):
                browser.navigate('http://127.0.0.1:1234/' if mode=='url' else 'https://example.test/next',lambda:mode=='cancel')
            bar.iface_value.SetValue.assert_not_called()

    def test_live_factory_reuses_bound_window(self):
        with patch('job_agent.browser_delivery.VisibleBrowser') as factory:
            planned=PlannedBrowser();first=planned('https://example.test/first');second=planned('https://example.test/second')
            self.assertIs(first,second);factory.assert_called_once_with('https://example.test/first')
            first.navigate.assert_called_once_with('https://example.test/second',planned.cancelled)

    def test_profile_source_url_cannot_match_only_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);profiles=root/'form-profiles';profiles.mkdir()
            profile=profile_fixture();(profiles/'one.json').write_text(json.dumps(profile),encoding='utf-8')
            store=Mock();action={'kind':'external_submit','target_url':profile['url'],'job_id':'synthetic'}
            def source(link):
                store.job.return_value={'detail':{'research':{'pages':[{'url':profile['identity_source_url'],'text':profile['identity_evidence_quote']+' '+link}]}}}
            source(profile['url']+'?different=1')
            with self.assertRaises(DeliveryBlocked):adapter_for(action,root,store)
            source(profile['url'])
            with patch('job_agent.delivery_worker.read_identity',return_value={}):
                self.assertEqual(adapter_for(action,root,store).adapter_id,'form:synthetic')

if __name__=='__main__':unittest.main()
