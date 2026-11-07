"""Read bounded public pages without cookies or credentials, pinning validated IPs."""
from __future__ import annotations

import gzip
from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urljoin, urlsplit, urlunsplit


class SourceError(ValueError):
    pass


def public_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
        raise SourceError('来源必须是公开 http(s) 地址。')
    if p.port not in {None, 80 if p.scheme == 'http' else 443}:
        raise SourceError('来源使用了不允许的端口。')
    if p.hostname.lower() in {'localhost', 'metadata.google.internal'} or p.hostname.lower().endswith(('.local', '.internal', '.localhost')):
        raise SourceError('不读取内部网络地址。')
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SourceError('不读取内部网络地址。')
    if len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise SourceError('来源链接不合法。')
    return urlunsplit((p.scheme, p.netloc, p.path or '/', p.query, ''))


def resolve_public(host: str, port: int) -> list[str]:
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError:
        raise SourceError('来源域名无法解析。') from None
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise SourceError('来源域名解析到了非公网地址。')
    return addresses


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'svg'}:
            self.hidden += 1
        if tag in {'p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript', 'svg'} and self.hidden:
            self.hidden -= 1
        if tag in {'p', 'div', 'li', 'tr'} and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def fetch_public_text(url: str, timeout: float = 12) -> dict:
    original = public_url(url)
    url = original
    for _ in range(4):
        p = urlsplit(public_url(url))
        host = p.hostname.encode('idna').decode('ascii')
        port = p.port or (443 if p.scheme == 'https' else 80)
        addresses = resolve_public(host, port)
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        sock = None
        try:
            sock = socket.create_connection((addresses[0], port), timeout=timeout)
            if p.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            path = urlunsplit(('', '', p.path or '/', p.query, ''))
            connection.request('GET', path, headers={'User-Agent': 'JobAgent-SourceReader/0.1',
                                                     'Accept': 'text/html,text/plain', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise SourceError('来源重定向缺少地址。')
                url = public_url(urljoin(url, location))
                continue
            if response.status != 200:
                raise SourceError('来源返回 HTTP ' + str(response.status))
            content_type = response.getheader('Content-Type', '').lower()
            if not (content_type.startswith('text/html') or content_type.startswith('text/plain') or content_type.startswith('application/xhtml+xml')):
                raise SourceError('来源不是可读取的网页文本。')
            if response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}:
                raise SourceError('来源返回了不支持的压缩格式。')
            raw = response.read(1_500_001)
            if len(raw) > 1_500_000:
                raise SourceError('来源网页过大。')
            charset = re.search(r'charset=[\"\x27]?([a-zA-Z0-9_-]+)', content_type)
            try:
                text = raw.decode(charset.group(1) if charset else 'utf-8', errors='replace')
            except LookupError:
                raise SourceError('来源编码不受支持。') from None
            if 'html' in content_type:
                parser = PageText()
                parser.feed(text)
                text = ''.join(parser.parts)
            text = '\n'.join(re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip())
            if len(text) < 50:
                raise SourceError('来源文字过少，可能需要 JavaScript 或登录。')
            return {'url': original, 'final_url': url, 'text': text[:120000]}
        except (OSError, http.client.HTTPException, UnicodeError):
            raise SourceError('来源网络连接失败或超时。') from None
        finally:
            connection.close()
            if sock is not None:
                sock.close()
    raise SourceError('来源重定向次数过多。')


def quote_present(quote: str, page: str) -> bool:
    normalize = lambda text: re.sub(r'\s+', '', text).casefold()
    needle = normalize(quote)
    return len(needle) >= 8 and needle in normalize(page)
