"""Probe an explicitly configured model API using synthetic data only."""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import getpass
import io
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ProbeError(Exception):
    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ProbeError('Redirect refused; supply the final API base URL.', code)


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip('/')
    parts = urllib.parse.urlsplit(value)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Use a base URL without credentials, query parameters or fragments.')
    local = parts.hostname in {'localhost', '127.0.0.1', '::1'}
    if parts.scheme != 'https' and not (parts.scheme == 'http' and local):
        raise ValueError('Use HTTPS, or HTTP only for a local loopback endpoint.')
    for suffix in ('/chat/completions', '/responses'):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    return value


def post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProbeError('Response exceeds the 4 MB diagnostic limit.')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ProbeError('Expected a JSON object from the API.')
        return result
    except urllib.error.HTTPError as exc:
        # Never print error bodies: some gateways echo credentials or request headers.
        exc.close()
        hints = {
            400: 'Request rejected; check protocol, model and optional parameter support.',
            401: 'Authentication failed; check the API key.',
            403: 'Access denied; check model or account permissions.',
            404: 'Endpoint or model not found; check base URL and model ID.',
            405: 'Endpoint does not accept this method.',
            413: 'Image or request too large for this endpoint.',
            422: 'Request schema rejected by this endpoint.',
            429: 'Rate limit or quota reached; no retry was sent.',
        }
        raise ProbeError(hints.get(exc.code, 'Server returned an HTTP error.'), exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProbeError('Connection failed or timed out; verify network and endpoint.') from None
    except (ValueError, UnicodeError):
        raise ProbeError('Endpoint returned invalid JSON, possibly an HTML login page.') from None


def extract_text(data: dict, protocol: str) -> str:
    if protocol == 'chat':
        choices = data.get('choices') or []
        if not choices or choices[0].get('finish_reason') == 'length':
            raise ProbeError('Missing or truncated chat output.')
        content = choices[0].get('message', {}).get('content', '')
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return ''.join(x.get('text', '') for x in content if x.get('type') == 'text').strip()
    else:
        if data.get('status') in {'failed', 'incomplete', 'cancelled'}:
            raise ProbeError('Responses API output was not completed.')
        output = data.get('output') or []
        return ''.join(
            part.get('text', '') for item in output if item.get('type') == 'message'
            for part in item.get('content', []) if part.get('type') == 'output_text'
        ).strip()
    raise ProbeError('Response has no supported text field.')


def object_schema(properties: dict) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def build_payload(protocol: str, model: str, prompt: str, *, image: str | None = None,
                  schema: dict | None = None, function: dict | None = None,
                  max_tokens: int = 2048, chat_token_field: str = 'max_completion_tokens') -> dict:
    payload: dict[str, Any] = {'model': model, 'stream': False}
    if protocol == 'chat':
        content: Any = prompt
        if image:
            content = [{'type': 'text', 'text': prompt},
                       {'type': 'image_url', 'image_url': {'url': image}}]
        payload['messages'] = [{'role': 'user', 'content': content}]
        payload[chat_token_field] = max_tokens
        if schema:
            payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'capability_probe', 'strict': True, 'schema': schema}}
        if function:
            payload['tools'] = [{'type': 'function', 'function': function}]
            payload['tool_choice'] = {'type': 'function', 'function': {'name': function['name']}}
    elif protocol == 'responses':
        content = [{'type': 'input_text', 'text': prompt}]
        if image:
            content.append({'type': 'input_image', 'image_url': image})
        payload.update({'input': [{'role': 'user', 'content': content}],
                        'max_output_tokens': max_tokens, 'store': False})
        if schema:
            payload['text'] = {'format': {'type': 'json_schema', 'name': 'capability_probe',
                                          'strict': True, 'schema': schema}}
        if function:
            payload['tools'] = [{'type': 'function', **function}]
            payload['tool_choice'] = {'type': 'function', 'name': function['name']}
    else:
        raise ValueError('Unknown API protocol.')
    return payload


def synthetic_screenshot() -> tuple[str, dict]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ProbeError('Pillow is missing; install requirements-probe.txt.') from None
    code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
    employees = 20 + secrets.randbelow(481)
    image = Image.new('RGB', (1024, 480), '#f3f5f7')
    draw = ImageDraw.Draw(image)
    font_paths = [Path('C:/Windows/Fonts/msyh.ttc'), Path('C:/Windows/Fonts/arial.ttf'),
                  Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    path = next((p for p in font_paths if p.exists()), None)
    font = ImageFont.truetype(str(path), 30) if path else ImageFont.load_default(size=30)
    chinese = bool(path and path.name == 'msyh.ttc')
    roles = ['AI 应用开发实习生', '大模型算法实习生', 'AI 产品实习生'] if chinese else [
        'AI Application Intern', 'AI Algorithm Intern', 'AI Product Intern']
    title = secrets.choice(roles)
    lines = [title, 'Synthetic test company (not a real job)', 'Location: Remote',
             f'Employees: {employees}', f'Verification code: {code}', 'Skills: Python / RAG / Agents']
    for index, text in enumerate(lines):
        draw.text((36, 28 + index * 65), text, fill='#16243a', font=font)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    data_url = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    return data_url, {'role': title, 'code': code, 'employees': employees}


def exact_object(text: str, expected: dict) -> bool:
    try:
        actual = json.loads(text)
    except (TypeError, ValueError):
        return False
    return (isinstance(actual, dict) and actual.keys() == expected.keys()
            and all(type(actual[k]) is type(v) and actual[k] == v for k, v in expected.items()))


def tool_matches(data: dict, protocol: str, expected: dict) -> bool:
    if protocol == 'chat':
        choices = data.get('choices') or []
        if not choices or choices[0].get('finish_reason') in {'length', 'content_filter'}:
            return False
        calls = choices[0].get('message', {}).get('tool_calls', [])
        calls = [x.get('function', {}) for x in calls if x.get('type') == 'function']
    else:
        if data.get('status') in {'failed', 'incomplete', 'cancelled', 'queued', 'in_progress'}:
            return False
        calls = [x for x in data.get('output', []) if x.get('type') == 'function_call']
    return len(calls) == 1 and calls[0].get('name') == 'record_test_job' and exact_object(
        calls[0].get('arguments', ''), expected)


@dataclass
class Result:
    capability: str
    protocol: str
    status: str
    detail: str
    seconds: float
    http_status: int | None = None


class Probe:
    def __init__(self, base_url: str, model: str, key: str, timeout: float = 60,
                 max_tokens: int = 2048, chat_token_field: str = 'max_completion_tokens',
                 transport=post_json, on_result=None, cancelled=None):
        self.base_url = normalize_base_url(base_url)
        self.model, self.key, self.timeout = model, key, timeout
        self.max_tokens, self.chat_token_field = max_tokens, chat_token_field
        self.transport = transport
        self.on_result = on_result
        self.cancelled = cancelled or (lambda: False)
        self.results: list[Result] = []

    def request(self, protocol: str, prompt: str, **kwargs) -> dict:
        payload = build_payload(protocol, self.model, prompt, max_tokens=self.max_tokens,
                                chat_token_field=self.chat_token_field, **kwargs)
        suffix = '/chat/completions' if protocol == 'chat' else '/responses'
        return self.transport(self.base_url + suffix, self.key, payload, self.timeout)

    def check(self, name: str, protocol: str, action) -> Result:
        started = time.monotonic()
        try:
            passed = action()
            result = Result(name, protocol, 'pass' if passed else 'fail',
                            'Observed expected output in one synthetic trial.' if passed
                            else 'Output did not match the synthetic test; inspect compatibility.',
                            round(time.monotonic() - started, 2))
        except ProbeError as exc:
            result = Result(name, protocol, 'error', str(exc),
                            round(time.monotonic() - started, 2), exc.status)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            result = Result(name, protocol, 'error', 'Unexpected API response shape.',
                            round(time.monotonic() - started, 2))
        self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)
        else:
            print(f'[{result.status.upper()}] {protocol}/{name}: {result.detail}', flush=True)
        return result

    def run(self, protocol: str = 'auto') -> dict:
        if protocol not in {'auto', 'chat', 'responses'}:
            raise ValueError('Unknown API protocol.')
        self.results = []
        selected = None
        for candidate in (['chat', 'responses'] if protocol == 'auto' else [protocol]):
            if self.cancelled():
                break
            nonce = secrets.token_hex(8)
            result = self.check('text', candidate, lambda: extract_text(self.request(
                candidate, 'Return exactly this token with no other text: ' + nonce), candidate) == nonce)
            if result.status == 'pass':
                selected = candidate
                break
            if result.http_status in {401, 403, 429} or (result.status == 'error' and result.http_status is None):
                break
        if selected:
            def vision():
                image, expected = synthetic_screenshot()
                prompt = ('Read ONLY the attached synthetic job screenshot. Return a JSON object with '
                          'exactly three keys: role (copy the visible job title in its original language), '
                          'code (the visible verification code, string), employees '
                          '(the visible employee count, integer). No Markdown or other text.')
                return exact_object(extract_text(self.request(selected, prompt, image=image), selected), expected)
            def structured():
                schema = object_schema({'role': {'type': 'string', 'enum': ['intern']},
                                        'employees': {'type': 'integer'}, 'remote': {'type': 'boolean'}})
                data = self.request(selected, 'Extract as JSON: role is intern, company has 86 employees, '
                                    'and remote work is allowed.', schema=schema)
                return exact_object(extract_text(data, selected), {'role': 'intern', 'employees': 86, 'remote': True})
            def tools():
                job_id = secrets.token_hex(6)
                function = {'name': 'record_test_job', 'description': 'Test only. No action is executed.',
                            'strict': True, 'parameters': object_schema({
                                'job_id': {'type': 'string'}, 'interested': {'type': 'boolean'}})}
                data = self.request(selected, f'Call record_test_job with job_id {job_id} and interested true. '
                                    'This is a capability test only.', function=function)
                return tool_matches(data, selected, {'job_id': job_id, 'interested': True})
            for name, action in [('vision', vision), ('structured_output', structured), ('tool_call', tools)]:
                if self.cancelled():
                    break
                result = self.check(name, selected, action)
                if result.http_status in {401, 403, 429}:
                    break
        passed = {r.capability for r in self.results if r.protocol == selected and r.status == 'pass'}
        return {
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'api_origin': urllib.parse.urlsplit(self.base_url).netloc,
            'model': self.model, 'selected_protocol': selected,
            'cancelled': bool(self.cancelled()),
            'text_and_vision_passed': {'text', 'vision'} <= passed,
            'all_four_checks_passed': len(passed) == 4,
            'not_tested': ['web_search', 'computer_control', 'job_site_permissions', 'production_reliability'],
            'note': 'A passing sample does not prove universal schema enforcement or production reliability. '
                    'An error is not proof that the provider lacks the capability. No tool was executed.',
            'results': [asdict(r) for r in self.results],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.environ.get('JOB_AGENT_BASE_URL'))
    parser.add_argument('--model', default=os.environ.get('JOB_AGENT_MODEL'))
    parser.add_argument('--protocol', choices=['auto', 'chat', 'responses'], default='auto')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--max-output-tokens', type=int, default=2048)
    parser.add_argument('--chat-token-field', choices=['max_completion_tokens', 'max_tokens'],
                        default='max_completion_tokens')
    parser.add_argument('--key-file', type=Path, help='Optional local UTF-8 key file; contents are never logged.')
    parser.add_argument('--no-key', action='store_true', help='Only for a local unauthenticated model server.')
    parser.add_argument('--report', type=Path, default=ROOT / 'private' / 'api-capabilities.json')
    args = parser.parse_args(argv)
    if args.key_file and args.no_key:
        parser.error('--key-file and --no-key cannot be combined.')
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_output_tokens <= 0:
        parser.error('Timeout and output token budget must be positive.')
    try:
        base_url = normalize_base_url(args.base_url or input('API Base URL (include /v1 if required): ').strip())
        model = args.model or input('Exact model ID: ').strip()
        if not model:
            raise ValueError('Model ID is required.')
        if args.no_key:
            if urllib.parse.urlsplit(base_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('--no-key is only permitted for loopback endpoints.')
            key = ''
        elif args.key_file:
            key = args.key_file.read_text(encoding='utf-8-sig').strip()
        else:
            key = os.environ.get('JOB_AGENT_API_KEY', '')
            if not key:
                if not sys.stdin.isatty():
                    raise ValueError('Use JOB_AGENT_API_KEY or --key-file outside an interactive terminal.')
                key = getpass.getpass('API key (hidden, not saved): ').strip()
        if not key and not args.no_key:
            raise ValueError('API key is empty.')
        print('Only synthetic text and one generated image will be sent. No resume or desktop data is read.')
        print('Up to five requests; no retries. Tool-call output is inspected but never executed.')
        probe = Probe(base_url, model, key, args.timeout, args.max_output_tokens, args.chat_token_field)
        report = probe.run(args.protocol)
        # Defense in depth against credentials echoed through configurable metadata.
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if key:
            serialized = serialized.replace(json.dumps(key, ensure_ascii=False)[1:-1], '[REDACTED]')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized, encoding='utf-8')
        print('Report:', args.report)
        return 0 if report['all_four_checks_passed'] else 1
    except (ValueError, OSError) as exc:
        if isinstance(exc, OSError):
            print('Local file operation failed; check the key-file/report path and permissions.', file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('Cancelled. No job applications were sent.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
