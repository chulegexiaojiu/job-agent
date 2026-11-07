"""Deterministic tests of bounded browser collection; never contact real sites."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from job_agent.app import Workbench
from job_agent.browser_batch import (BrowserSession, PageLoading, collect, contained, page_url,
                                     read_capture, select_candidates, wait_for_capture)
from job_agent.browser_capture import CaptureError, CaptureStopped


def capture_fixture(directory, url):
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / 'job-evidence.png'
    picture = Image.new('RGB', (600, 400), 'white')
    ImageDraw.Draw(picture).line((20, 20, 500, 20), fill='black')
    picture.save(image)
    text = '合成测试 AI实习生 20-99人。只用于离线测试。'
    data = {'url': url, 'text': text, 'screenshot': str(image.resolve()), 'sent': False,
            'captured_at': '2026-01-01T00:00:00Z',
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'screenshot_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}
    (directory / 'capture.json').write_text(json.dumps(data), encoding='utf-8')
    return data


class FakeSession:
    origin = 'https://www.zhipin.com/web/geek/jobs'
    def __init__(self, failures=None):
        self.opened = []
        self.failures = failures or {}
    def candidates(self, known, limit):
        return ([{'url': f'https://www.zhipin.com/job_detail/test{i}.html'} for i in range(limit)], 2)
    def open_detail(self, url):
        self.opened.append(url)
        self.current = url
    def capture(self, directory):
        failure = self.failures.get(len(self.opened))
        if failure:
            raise failure
        return capture_fixture(directory, self.current)


class BatchCaptureTests(unittest.TestCase):
    def test_only_visible_safe_unique_unknown_jobs_selected(self):
        def link(job, y=10, **changes):
            return {'url': f'https://www.zhipin.com/job_detail/{job}.html?ka=x', 'label': job,
                    'rect': (10, y, 90, y + 20), 'class': 'job-name', **changes}
        links = [link('known'), link('a_b-c'), link('a_b-c'), link('offscreen', 100),
                 link('unsafe', url='https://other.test/job_detail/unsafe.html'),
                 link('button', **{'class': 'chat-button'}), link('next')]
        selected, duplicates = select_candidates(links, (0, 0, 100, 100),
                                                  {'https://www.zhipin.com/job_detail/known.html'}, 1)
        self.assertEqual(selected, [{'url': 'https://www.zhipin.com/job_detail/a_b-c.html', 'label': 'a_b-c'}])
        self.assertEqual(duplicates, 1)
        self.assertFalse(contained((1, -1, 20, 20), (0, 0, 100, 100)))
        for count in [0, 6, True, -1]:
            with self.assertRaises(CaptureError):
                select_candidates([], (0, 0, 100, 100), set(), count)

    def test_page_allowlist_rejects_login_verification_and_external(self):
        self.assertEqual(page_url('https://www.zhipin.com/web/geek/jobs?query=AI'), FakeSession.origin)
        for url in ['https://www.zhipin.com/web/user/login', 'https://www.zhipin.com/captcha',
                    'https://other.test/web/geek/jobs', 'file:///test', 'https://u:p'+'@www.zhipin.com/web/geek/jobs']:
            with self.assertRaises(CaptureStopped):
                page_url(url)

    def test_single_failure_does_not_block_next_and_manifest_is_incremental(self):
        with tempfile.TemporaryDirectory() as temp:
            session = FakeSession({1: CaptureError('区域不完整')})
            report = collect(Path(temp), session, set(), 3)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(len(report['captures']), 2)
            self.assertEqual(len(report['failures']), 1)
            self.assertEqual(len(session.opened), 3)
            saved = json.loads((Path(temp) / 'batch.json').read_text(encoding='utf-8'))
            self.assertEqual(saved, report)
            self.assertFalse(saved['sent'])

    def test_restriction_stops_batch_and_preserves_completed_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            session = FakeSession({2: CaptureStopped('安全验证')})
            report = collect(Path(temp), session, set(), 3)
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(len(session.opened), 2)
            self.assertEqual(len(report['captures']), 1)
            self.assertEqual(report['reason'], '安全验证')

    def test_manifest_detects_tampered_text_image_and_source_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for field in ['text', 'screenshot_sha256', 'screenshot', 'sent']:
                data = capture_fixture(base / field, 'https://www.zhipin.com/job_detail/test.html')
                path = base / field / 'capture.json'
                self.assertEqual(read_capture(path)['url'], data['url'])
                data[field] = {'text': 'changed', 'screenshot_sha256': 'bad',
                               'screenshot': str(base / 'outside.png'), 'sent': True}[field]
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(CaptureError):
                    read_capture(path)

    def test_wrong_target_manifest_stops_import(self):
        with tempfile.TemporaryDirectory() as temp:
            session = FakeSession()
            session.capture = lambda folder: capture_fixture(folder, 'https://www.zhipin.com/job_detail/other.html')
            report = collect(Path(temp), session, set(), 2)
            self.assertEqual(report['status'], 'stopped')
            self.assertEqual(report['captures'], [])
            self.assertEqual(len(session.opened), 1)

    def test_completed_batch_imports_once_and_stopped_batch_never_starts(self):
        for status in ['completed', 'stopped', 'cancelled']:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                private = Path(temp)
                batch = private / 'captures' / 'batch'
                report = collect(batch, FakeSession(), set(), 2)
                report['status'] = status
                path = batch / 'batch.json'
                path.write_text(json.dumps(report), encoding='utf-8')
                root = tk.Tk(); root.withdraw()
                app = Workbench(root, private)
                try:
                    with patch.object(app, 'start') as start:
                        app.capturing = True
                        app.events.put(('batch_capture_ready', str(path)))
                        root.after_cancel(app.poll_id); app.drain()
                        self.assertEqual(len(app.store.jobs()), 2)
                        self.assertFalse(app.capturing)
                        self.assertEqual(start.call_count, int(status == 'completed'))
                        app.events.put(('batch_capture_ready', str(path)))
                        root.after_cancel(app.poll_id); app.drain()
                        self.assertEqual(len(app.store.jobs()), 2)
                        self.assertEqual(start.call_count, int(status == 'completed'))
                finally:
                    app.close()

    def test_navigation_waits_for_transient_empty_tree_only_for_expected_target(self):
        session = BrowserSession.__new__(BrowserSession)
        session.expected = 'https://www.zhipin.com/job_detail/test.html'
        session.window = Mock()
        session.guard = Mock()
        doc = Mock()
        node = Mock(); node.class_name.return_value = 'info-primary'
        doc.descendants.return_value = [node]
        session.current = Mock(side_effect=[PageLoading('loading'), (doc, session.expected), (doc, session.expected)])
        with patch('job_agent.browser_batch.time.sleep'), patch('job_agent.browser_batch.time.monotonic', side_effect=[0, .1, .2, .3, 1.2, 1.3]):
            session.open_detail(session.expected)
        session.window.type_keys.assert_called_once_with('^{HOME}', set_foreground=True)
        session.current = Mock(return_value=(doc, 'https://www.zhipin.com/web/user/login'))
        with self.assertRaises(CaptureStopped):
            session.open_detail(session.expected)

    def test_guard_detects_changed_search_query_and_restriction(self):
        session = BrowserSession.__new__(BrowserSession)
        session.expected = FakeSession.origin
        session.origin_value = FakeSession.origin + '?query=AI'
        session.current = Mock(side_effect=PageLoading('loading'))
        with self.assertRaises(CaptureStopped):
            session.guard()
        doc = Mock()
        session.current = Mock(return_value=(doc, FakeSession.origin + '?query=other'))
        with self.assertRaises(CaptureStopped):
            session.guard()
        session.current.return_value = (doc, session.origin_value)
        warning = Mock(); warning.window_text.return_value = '请完成安全验证'
        doc.descendants.return_value = [warning]
        with patch('job_agent.browser_batch.rect', return_value=(0,0,100,100)):
            with self.assertRaises(CaptureStopped):
                session.guard()

    def test_workbench_rejects_capture_for_another_target(self):
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp)
            capture_fixture(private / 'capture', 'https://www.zhipin.com/job_detail/a.html')
            root = tk.Tk(); root.withdraw()
            app = Workbench(root, private)
            try:
                with self.assertRaises(ValueError):
                    app.import_capture(private/'capture/capture.json', 'https://www.zhipin.com/job_detail/b.html')
                self.assertEqual(app.store.jobs(), [])
            finally:
                app.close()

    def test_wait_cancel_and_timeout_terminate_and_reap_child(self):
        for cancel in [True, False]:
            process = Mock()
            process.poll.return_value = None
            process.communicate.return_value = ('', '')
            output, reason = wait_for_capture(process, -1 if not cancel else 10, lambda: cancel)
            self.assertEqual(output, '')
            self.assertEqual(reason, 'cancelled' if cancel else 'timeout')
            process.terminate.assert_called_once()
            process.communicate.assert_called_once_with(timeout=5)

    def test_wait_failure_reaps_child_and_kills_if_terminate_stalls(self):
        process = Mock()
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired('capture', .3),
                                          subprocess.TimeoutExpired('capture', 5), ('', '')]
        with self.assertRaises(ValueError):
            wait_for_capture(process, 10, lambda: False, Mock(side_effect=ValueError('bad report')))
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 3)

    def test_wait_success_does_not_terminate_finished_process(self):
        process = Mock()
        process.poll.return_value = 0
        process.communicate.return_value = ('done', '')
        self.assertEqual(wait_for_capture(process, 10, lambda: False), ('done', ''))
        process.terminate.assert_not_called()

    def test_user_cancelled_completed_batch_does_not_start_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp)
            batch = private / 'captures' / 'batch'
            collect(batch, FakeSession(), set(), 1)
            root = tk.Tk(); root.withdraw()
            app = Workbench(root, private)
            try:
                with patch.object(app, 'start') as start:
                    app.cancelled.set()
                    app.events.put(('batch_capture_ready', str(batch / 'batch.json')))
                    root.after_cancel(app.poll_id); app.drain()
                    start.assert_not_called()
                    self.assertEqual(len(app.store.jobs()), 1)
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
