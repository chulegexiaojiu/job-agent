"""Offline tests; a fake service is not evidence of model capability."""
import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tools.check_api import (NoRedirect, Probe, ProbeError, build_payload, exact_object,
                             extract_text, main, normalize_base_url, post_json,
                             synthetic_screenshot, tool_matches)


def chat_text(text):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}


def responses_text(text):
    return {'status': 'completed', 'output': [
        {'type': 'reasoning', 'summary': []},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}


class ProbeTests(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(normalize_base_url('https://example.test/v1/chat/completions/'),
                         'https://example.test/v1')
        self.assertEqual(normalize_base_url('http://localhost:1234/v1/responses'),
                         'http://localhost:1234/v1')

    def test_rejects_unsafe_or_credential_urls(self):
        for url in ['http://example.test/v1', 'https://user:secret@example.test/v1',
                    'https://example.test/v1?key=secret', 'https://example.test/#fragment',
                    'file:///tmp/model', 'not-a-url']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_base_url(url)

    def test_chat_wire_format(self):
        data = build_payload('chat', 'test', 'p', image='data:image/png;base64,AA==',
                             schema={'type': 'object'})
        self.assertEqual(data['messages'][0]['content'][1]['type'], 'image_url')
        self.assertTrue(data['response_format']['json_schema']['strict'])
        self.assertNotIn('input', data)
        legacy = build_payload('chat', 'test', 'p', chat_token_field='max_tokens')
        self.assertIn('max_tokens', legacy)
        self.assertNotIn('max_completion_tokens', legacy)

    def test_responses_wire_format(self):
        function = {'name': 'test_function', 'parameters': {'type': 'object'}}
        data = build_payload('responses', 'test', 'p', image='data:image/png;base64,AA==',
                             schema={'type': 'object'}, function=function)
        self.assertFalse(data['store'])
        self.assertEqual(data['input'][0]['content'][1]['type'], 'input_image')
        self.assertEqual(data['tools'][0]['name'], 'test_function')
        self.assertNotIn('function', data['tools'][0])
        self.assertIn('format', data['text'])

    def test_parses_both_text_formats(self):
        self.assertEqual(extract_text(chat_text('hello'), 'chat'), 'hello')
        self.assertEqual(extract_text(responses_text('hello'), 'responses'), 'hello')

    def test_rejects_incomplete_output(self):
        data = chat_text('partial')
        data['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ProbeError):
            extract_text(data, 'chat')
        with self.assertRaises(ProbeError):
            extract_text({'status': 'incomplete'}, 'responses')

    def test_json_semantics_and_types_are_checked(self):
        self.assertTrue(exact_object('{"employees":86}', {'employees': 86}))
        for output in ['{"employees":"86"}', '{"employees":true}',
                       '{"employees":86,"extra":1}', '```json {} ```', '[]']:
            self.assertFalse(exact_object(output, {'employees': 86}))
        self.assertFalse(exact_object('{"x":1}', {'x': True}))

    def test_tool_name_arguments_and_count_checked(self):
        expected = {'job_id': 'abc', 'interested': True}
        call = {'type': 'function_call', 'name': 'record_test_job',
                'arguments': json.dumps(expected)}
        self.assertTrue(tool_matches({'output': [call]}, 'responses', expected))
        self.assertFalse(tool_matches({'output': [call, call]}, 'responses', expected))
        self.assertFalse(tool_matches({'output': [{**call, 'name': 'run_shell'}]}, 'responses', expected))

    def test_synthetic_image_contains_random_values(self):
        from PIL import Image
        image, expected = synthetic_screenshot()
        raw = base64.b64decode(image.split(',', 1)[1])
        with Image.open(io.BytesIO(raw)) as png:
            self.assertEqual(png.size, (1024, 480))
        self.assertEqual(len(expected['code']), 8)
        self.assertTrue(20 <= expected['employees'] <= 500)
        self.assertTrue(expected['role'])

    def test_both_protocol_flows_against_fake_responses(self):
        image_expected = {'role': 'AI Application Intern', 'code': 'RANDOM42', 'employees': 123}
        for protocol in ['chat', 'responses']:
            requests = []
            def transport(url, key, payload, timeout):
                requests.append(payload)
                if protocol == 'chat':
                    content = payload['messages'][0]['content']
                    wrapper = chat_text
                else:
                    content = payload['input'][0]['content']
                    wrapper = responses_text
                prompt = content if isinstance(content, str) else content[0]['text']
                if 'tools' in payload:
                    job_id = prompt.split('job_id ')[1].split(' ')[0]
                    arguments = json.dumps({'job_id': job_id, 'interested': True})
                    function = {'name': 'record_test_job', 'arguments': arguments}
                    if protocol == 'chat':
                        return {'choices': [{'message': {'tool_calls': [
                            {'type': 'function', 'function': function}]}}]}
                    return {'output': [{'type': 'function_call', **function}]}
                if 'response_format' in payload or 'text' in payload:
                    return wrapper('{"role":"intern","employees":86,"remote":true}')
                if 'Read ONLY' in prompt:
                    self.assertNotIn(image_expected['code'], prompt)
                    self.assertNotIn(str(image_expected['employees']), prompt)
                    return wrapper(json.dumps(image_expected))
                return wrapper(prompt.rsplit(': ', 1)[1])
            with patch('tools.check_api.synthetic_screenshot', return_value=(
                    'data:image/png;base64,AA==', image_expected)), contextlib.redirect_stdout(io.StringIO()):
                report = Probe('https://example.test/v1', 'fake', 'secret', transport=transport).run(protocol)
            self.assertTrue(report['all_four_checks_passed'])
            self.assertEqual(len(requests), 4)
            self.assertNotIn('secret', json.dumps(report))
            self.assertIn('computer_control', report['not_tested'])

    def test_auto_protocol_falls_back_after_404(self):
        calls = []
        def transport(url, key, payload, timeout):
            calls.append(url)
            if url.endswith('/chat/completions'):
                raise ProbeError('not found', 404)
            raise ProbeError('not configured', 401)
        with contextlib.redirect_stdout(io.StringIO()):
            report = Probe('https://example.test/v1', 'fake', 'secret', transport=transport).run()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(report['selected_protocol'])

    def test_auth_and_quota_errors_stop_without_retries(self):
        for status in [401, 403, 429]:
            calls = []
            def transport(*args):
                calls.append(args)
                raise ProbeError('Rejected', status)
            with contextlib.redirect_stdout(io.StringIO()):
                report = Probe('https://example.test/v1', 'fake', 'secret', transport=transport).run()
            self.assertEqual(len(calls), 1)
            self.assertFalse(report['text_and_vision_passed'])

    def test_report_metadata_redacts_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = 'private-synthetic-key'
            (root / 'key.txt').write_text(key, encoding='utf-8')
            report = root / 'report.json'
            with patch.object(Probe, 'run', return_value={
                    'model': key, 'all_four_checks_passed': False}), contextlib.redirect_stdout(io.StringIO()):
                code = main(['--base-url', 'https://example.test/v1', '--model', key,
                             '--key-file', str(root / 'key.txt'), '--report', str(report)])
            self.assertEqual(code, 1)
            self.assertNotIn(key, report.read_text(encoding='utf-8'))


class LocalHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                cls.requests.append((self.path, self.headers.get('Authorization')))
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path == '/redirect':
                    self.send_response(307)
                    self.send_header('Location', '/target')
                    self.end_headers()
                    return
                status = 401 if self.path == '/auth' else 200
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if self.path == '/auth':
                    self.wfile.write(b'{"error":"secret-synthetic-token"}')
                elif self.path == '/html':
                    self.wfile.write(b'<html>login</html>')
                else:
                    self.wfile.write(b'{"ok":true}')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_real_local_http_transport(self):
        result = post_json(self.origin + '/ok', 'test-key', {'test': True}, 2)
        self.assertTrue(result['ok'])
        self.assertEqual(self.requests[-1], ('/ok', 'Bearer test-key'))

    def test_redirect_is_not_followed(self):
        start = len(self.requests)
        with self.assertRaises(ProbeError):
            post_json(self.origin + '/redirect', 'test-key', {}, 2)
        self.assertEqual(len(self.requests) - start, 1)

    def test_error_body_does_not_expose_credentials(self):
        with self.assertRaises(ProbeError) as caught:
            post_json(self.origin + '/auth', 'secret-synthetic-token', {}, 2)
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn('secret-synthetic-token', str(caught.exception))

    def test_html_endpoint_is_not_misreported_as_success(self):
        with self.assertRaises(ProbeError):
            post_json(self.origin + '/html', '', {}, 2)


if __name__ == '__main__':
    unittest.main()
