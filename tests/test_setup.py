"""Local setup tests. No external service or real API credential is used."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from job_agent.settings import (Connection, SecretError, discover_connection, forget_key,
                                load_connection, load_key, save_connection, save_key)
from job_agent.setup_ui import SetupApp
from tools.check_api import Probe, Result


class SettingsTests(unittest.TestCase):
    def test_only_connection_metadata_is_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'models.json').write_text(json.dumps({'providers': {'test': {
                'baseUrl': 'https://example.test/v1', 'api': 'openai-completions',
                'apiKey': '!this-command-must-never-execute',
                'models': [{'id': 'test-model', 'input': ['text', 'image']}]}}}))
            (root / 'settings.json').write_text(json.dumps({'defaultProvider': 'test'}))
            connection = discover_connection(root, 'test-model')
            self.assertEqual(connection.base_url, 'https://example.test/v1')
            self.assertEqual(connection.model, 'test-model')
            save_connection(connection, root / 'connection.json')
            self.assertNotIn('apiKey', (root / 'connection.json').read_text())
            self.assertEqual(load_connection(root / 'connection.json'), connection)

    def test_ambiguous_model_does_not_guess_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            providers = {name: {'baseUrl': f'https://{name}.test/v1',
                               'api': 'openai-completions', 'models': [{'id': 'same'}]}
                         for name in ['first', 'second']}
            (root / 'models.json').write_text(json.dumps({'providers': providers}))
            self.assertIsNone(discover_connection(root, 'same'))

    def test_invalid_settings_fail_without_saving(self):
        for fields in [{'timeout': float('nan')}, {'timeout': float('inf')},
                       {'max_output_tokens': 0}, {'protocol': 'unknown'},
                       {'model': ''}, {'base_url': 'https://user:password@example.test'}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Connection(**{'base_url': 'https://example.test/v1', 'model': 'test', **fields}).validated()

    @unittest.skipUnless(os.name == 'nt', 'Windows user-bound encryption')
    def test_encryption_round_trip_and_endpoint_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credential.dpapi'
            key = 'synthetic-only-credential-12345'
            save_key(key, 'https://example.test/v1', path)
            encrypted = path.read_bytes()
            self.assertNotIn(key.encode(), encrypted)
            self.assertEqual(load_key('https://example.test/v1/', path), key)
            self.assertIsNone(load_key('https://different.test/v1', path))
            self.assertIsNone(load_key('https://example.test/other', path))
            forget_key(path)
            self.assertIsNone(load_key('https://example.test/v1', path))

    @unittest.skipUnless(os.name == 'nt', 'Windows user-bound encryption')
    def test_corrupt_credential_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credential.dpapi'
            path.write_bytes(b'not-a-dpapi-blob')
            with self.assertRaises(SecretError):
                load_key('https://example.test/v1', path)


class SetupWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.connection = Connection('https://example.test/v1', 'fake-test-model')
        self.seen = []

    def tearDown(self):
        if hasattr(self, 'app') and not self.app.closed:
            self.app.close()
        else:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        self.temp.cleanup()

    def app_with(self, factory):
        self.app = SetupApp(self.root, private_dir=Path(self.temp.name),
                            initial_connection=self.connection, probe_factory=factory)
        self.app.remember.set(False)
        self.root.update()
        return self.app

    def finish(self):
        deadline = time.monotonic() + 3
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy, 'UI did not finish fake detection')

    def factory(self, *args, **kwargs):
        self.seen.append(args)
        owner = self
        class FakeProbe:
            def run(self, protocol):
                results = [Result(name, 'chat', 'pass', 'Synthetic fixture', 0.01)
                           for name in ['text', 'vision', 'structured_output', 'tool_call']]
                for result in results:
                    kwargs['on_result'](result)
                return {'model': args[1], 'selected_protocol': 'chat', 'cancelled': False,
                        'text_and_vision_passed': True, 'all_four_checks_passed': True,
                        'results': [r.__dict__ for r in results]}
        return FakeProbe()

    def test_paste_enter_path_runs_and_reports_without_saving_plaintext(self):
        app = self.app_with(self.factory)
        self.assertTrue(app.key_entry.bind('<Return>'))
        app.key_text.set('Bearer synthetic-key-for-ui-test')
        app.start()
        self.assertEqual(app.key_text.get(), '')
        app.start()  # A repeated Enter while busy must not launch twice.
        self.finish()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0][2], 'synthetic-key-for-ui-test')
        report = Path(self.temp.name) / 'api-capabilities.json'
        self.assertTrue(json.loads(report.read_text())['all_four_checks_passed'])
        self.assertIn('四项样本通过', app.status_text.get())
        for name in ['connection.json', 'api-capabilities.json']:
            self.assertNotIn('synthetic-key-for-ui-test', (Path(self.temp.name) / name).read_text())
        self.assertFalse((Path(self.temp.name) / 'credential.dpapi').exists())

    def test_empty_key_does_not_start_request(self):
        app = self.app_with(self.factory)
        app.start()
        self.assertFalse(app.busy)
        self.assertEqual(self.seen, [])
        self.assertIn('请粘贴', app.status_text.get())

    def test_auth_failure_cannot_save_bad_key_or_show_success(self):
        from tools.check_api import ProbeError
        def factory(*args, **kwargs):
            def reject(*_):
                raise ProbeError('Authentication failed', 401)
            return Probe(*args, **kwargs, transport=reject)
        app = self.app_with(factory)
        app.remember.set(True)
        app.key_text.set('invalid-synthetic-key')
        app.start()
        self.finish()
        self.assertFalse((Path(self.temp.name) / 'credential.dpapi').exists())
        self.assertIn('尚未通过', app.status_text.get())
        self.assertEqual(app.table.item('vision')['values'][1], '未执行')

    @unittest.skipUnless(os.name == 'nt', 'Windows user-bound encryption')
    def test_encrypted_key_reuse_and_clear(self):
        app = self.app_with(self.factory)
        app.remember.set(True)
        app.key_text.set('synthetic-encrypted-ui-key')
        app.start()
        self.finish()
        path = Path(self.temp.name) / 'credential.dpapi'
        self.assertEqual(load_key(self.connection.base_url, path), 'synthetic-encrypted-ui-key')
        app.start()
        self.finish()
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[-1][2], 'synthetic-encrypted-ui-key')
        app.forget()
        self.assertFalse(path.exists())


class CancellationTests(unittest.TestCase):
    def test_stop_does_not_send_remaining_requests(self):
        cancelled = threading.Event()
        calls = []
        def transport(url, key, payload, timeout):
            calls.append(url)
            nonce = payload['messages'][0]['content'].rsplit(': ', 1)[1]
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': nonce}}]}
        probe = Probe('https://example.test/v1', 'test', 'fake', transport=transport,
                      on_result=lambda result: cancelled.set(), cancelled=cancelled.is_set)
        report = probe.run('chat')
        self.assertEqual(len(calls), 1)
        self.assertTrue(report['cancelled'])
        self.assertFalse(report['all_four_checks_passed'])


if __name__ == '__main__':
    unittest.main()
