"""Offline checks for read-only browser import; do not open a real browser."""
from pathlib import Path
import hashlib
import json
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from job_agent.app import Workbench
from job_agent.browser_capture import CaptureError, compose_evidence, detail_url, validate_job_text
from job_agent.browser_batch import read_capture


class CaptureValidationTests(unittest.TestCase):
    def test_only_job_details_accepted_and_private_query_removed(self):
        self.assertEqual(detail_url('https://www.zhipin.com/job_detail/abc123.html?securityId=secret&ka=track#test'),
                         'https://www.zhipin.com/job_detail/abc123.html')
        self.assertEqual(detail_url('https://www.zhipin.com/job_detail/abc_12-3.html?ka=track'),
                         'https://www.zhipin.com/job_detail/abc_12-3.html')
        for url in ['file:///tmp/test', 'http://www.zhipin.com/job_detail/a.html',
                    'https://www.zhipin.com.evil.test/job_detail/a.html',
                    'https://www.zhipin.com/web/geek/jobs', 'https://user:pass'+'@www.zhipin.com/job_detail/a.html',
                    'https://www.zhipin.com:8443/job_detail/a.html', 'https://localhost/job_detail/a.html']:
            with self.subTest(url=url), self.assertRaises(CaptureError):
                detail_url(url)

    def test_crop_preserves_pixels_without_other_window_regions(self):
        image = Image.new('RGB', (600, 500), 'red')  # outside content must be discarded
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, 240, 140), fill='white')
        draw.rectangle((310, 100, 500, 240), fill='white')
        draw.line((50, 60, 150, 60), fill='black', width=3)
        draw.line((320, 120, 420, 120), fill='black', width=3)
        output = compose_evidence(image, (0, 0, 600, 500), (0, 0, 600, 500),
                                  [(40, 40, 240, 140), (310, 100, 500, 240)])
        self.assertNotIn((255, 0, 0), output.getdata())
        self.assertEqual(output.crop((16, 16, 216, 116)).tobytes(), image.crop((40, 40, 240, 140)).tobytes())
        with self.assertRaises(CaptureError):
            compose_evidence(image, (0, 0, 500, 500), (0, 0, 600, 500), [(40, 40, 240, 140)] * 2)
        with self.assertRaises(CaptureError):
            compose_evidence(image, (0, 0, 600, 500), (0, 80, 600, 500), [(40, 40, 240, 140)] * 2)

    def test_blank_browser_image_fails_instead_of_being_used(self):
        with self.assertRaises(CaptureError):
            compose_evidence(Image.new('RGB', (100, 100)), (0, 0, 100, 100), (0, 0, 100, 100), [(0, 0, 50, 50)] * 2)

    def test_incomplete_or_restricted_page_does_not_import(self):
        header = 'AI 开发实习生\n广州'
        company = '公司基本信息\n合成公司\n20-99人'
        description = '职位描述\n' + '使用 Python 构建测试工具，完成测试并解释实现过程。' * 3
        self.assertIn('合成公司', validate_job_text(header, company, description))
        for altered in [description + '\n请完成安全验证', '职位描述\n加载中']:
            with self.assertRaises(CaptureError):
                validate_job_text(header, company, altered)
        with self.assertRaises(CaptureError):
            validate_job_text(header, '公司基本信息\n合成公司', description)


class BrowserImportWindowTests(unittest.TestCase):
    def test_import_queues_once_and_starts_only_new_job(self):
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp)
            image = private / 'job-evidence.png'
            Image.new('RGB', (600, 400), 'white').save(image)
            capture = private / 'capture.json'
            capture.write_text(json.dumps({'url': 'https://www.zhipin.com/job_detail/fixture.html',
                                          'text': '合成公司 AI实习生 20-99人', 'screenshot': str(image),
                                          'captured_at': '2026-01-01T00:00:00Z', 'sent': False,
                                          'screenshot_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                                          'text_sha256': hashlib.sha256('合成公司 AI实习生 20-99人'.encode()).hexdigest()}), encoding='utf-8')
            root = tk.Tk(); root.withdraw()
            app = Workbench(root, private)
            try:
                with patch.object(app, 'start') as start:
                    app.capturing = True
                    app.events.put(('capture_ready', str(capture)))
                    root.after_cancel(app.poll_id); app.drain()
                    self.assertFalse(app.capturing)
                    self.assertEqual(len(app.store.jobs()), 1)
                    start.assert_called_once()
                    self.assertEqual(app.store.jobs()[0]['status'], 'queued')
                    self.assertTrue((private / 'jobs' / app.store.jobs()[0]['id'] / 'source-capture.json').exists())
                    app.events.put(('capture_ready', str(capture)))
                    root.after_cancel(app.poll_id); app.drain()
                    self.assertEqual(start.call_count, 1)
                    self.assertEqual(len(app.store.jobs()), 1)
                    app.events.put(('capture_error', '模拟截图失败'))
                    root.after_cancel(app.poll_id); app.drain()
                    self.assertEqual(app.status.get(), '模拟截图失败')
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
