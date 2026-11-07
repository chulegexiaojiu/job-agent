"""Public connection settings and Windows user-bound encrypted credential storage."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

from tools.check_api import normalize_base_url

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'private'


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class Connection:
    base_url: str
    model: str
    protocol: str = 'auto'
    chat_token_field: str = 'max_completion_tokens'
    timeout: float = 60
    max_output_tokens: int = 4096

    def validated(self) -> 'Connection':
        base = normalize_base_url(self.base_url)
        model = self.model.strip()
        if not model or len(model) > 256 or any(ord(c) < 32 for c in model):
            raise ValueError('请输入有效的模型名称。')
        if self.protocol not in {'auto', 'chat', 'responses'}:
            raise ValueError('接口协议无效。')
        if self.chat_token_field not in {'max_tokens', 'max_completion_tokens'}:
            raise ValueError('输出长度参数无效。')
        if not 1 <= float(self.timeout) <= 300 or not 128 <= int(self.max_output_tokens) <= 32768:
            raise ValueError('超时应在 1～300 秒内，输出长度应在 128～32768 内。')
        return Connection(base, model, self.protocol, self.chat_token_field,
                          float(self.timeout), int(self.max_output_tokens))


def load_connection(path: Path = PRIVATE / 'connection.json') -> Connection | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        allowed = Connection.__dataclass_fields__
        return Connection(**{k: v for k, v in data.items() if k in allowed}).validated()
    except (ValueError, TypeError, AttributeError, OSError):
        raise ValueError('本地接口配置损坏，请在“高级配置”中重新设置。') from None


def save_connection(connection: Connection, path: Path = PRIVATE / 'connection.json') -> None:
    data = json.dumps(asdict(connection.validated()), ensure_ascii=False, indent=2)
    atomic_write(path, data.encode('utf-8'))


def discover_connection(agent_dir: Path | None = None, model_hint: str | None = None) -> Connection | None:
    """Read connection metadata only; never evaluate config commands or copy credentials."""
    agent_dir = agent_dir or Path.home() / '.pi' / 'agent'
    try:
        catalog = json.loads((agent_dir / 'models.json').read_text(encoding='utf-8-sig'))
        settings_path = agent_dir / 'settings.json'
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.exists() else {}
        providers = catalog.get('providers', {})
        model_hint = model_hint or os.environ.get('PI_MODEL') or settings.get('defaultModel')
        candidates = []
        for name, provider in providers.items():
            for model in provider.get('models', []):
                api = model.get('api') or provider.get('api')
                if api not in {'openai-completions', 'openai-responses'}:
                    continue
                if model.get('id') == model_hint:
                    candidates.append((name, provider, model, api))
        preferred = [item for item in candidates if item[0] == settings.get('defaultProvider')]
        if len(preferred) == 1:
            candidates = preferred
        if len(candidates) != 1:
            return None
        _, provider, model, api = candidates[0]
        return Connection(provider['baseUrl'], model['id'],
                          'responses' if api == 'openai-responses' else 'auto').validated()
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


class SecretError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    if os.name != 'nt':
        raise SecretError('加密保存需要 Windows；当前系统只能在内存中使用 Key。')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    blob_pointer = ctypes.POINTER(_Blob)
    crypt.CryptProtectData.argtypes = [blob_pointer, wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [blob_pointer, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    # UI_FORBIDDEN only. Do not use LOCAL_MACHINE: bind to the current user.
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'Job Agent API credential',
                                   None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise SecretError('Windows 加密存储操作失败，请重新粘贴 Key，或取消加密保存。')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        if target.pbData:
            ctypes.memset(target.pbData, 0, target.cbData)
            kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))
        ctypes.memset(buffer, 0, len(buffer))


def save_key(key: str, base_url: str, path: Path = PRIVATE / 'credential.dpapi') -> None:
    if not key or len(key) > 16384 or any(ord(c) < 32 for c in key):
        raise ValueError('Key 为空或含有无效字符，请重新粘贴。')
    data = json.dumps({'version': 1, 'base_url': normalize_base_url(base_url), 'api_key': key}).encode('utf-8')
    atomic_write(path, _dpapi(data, decrypt=False))


def load_key(base_url: str, path: Path = PRIVATE / 'credential.dpapi') -> str | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 65536:
            raise SecretError('本地加密凭据异常，请重新粘贴 Key。')
        data = json.loads(_dpapi(path.read_bytes(), decrypt=True))
        if data.get('version') != 1 or data.get('base_url') != normalize_base_url(base_url):
            return None
        key = data.get('api_key')
        if not isinstance(key, str) or not key or len(key) > 16384:
            raise SecretError('本地加密凭据异常，请重新粘贴 Key。')
        return key
    except (OSError, ValueError, AttributeError, TypeError):
        raise SecretError('无法读取本地加密凭据，请重新粘贴 Key。') from None


def forget_key(path: Path = PRIVATE / 'credential.dpapi') -> None:
    path.unlink(missing_ok=True)
