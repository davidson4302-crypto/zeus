"""pageread: the two ways code reads a page for a watch (watches.py) or for the signed-in check (signedin.py).

Both only READ. Neither can click, type, submit, sign in or send anything, and neither takes an instruction from
the page it reads.

  read_web(url)     a public page by plain GET. No sign-in travels with it: no saved logins, no stored site data, a
                    generic browser name. Every hop of a redirect is checked against the same address rule as the
                    first address, and a name that resolves to this Mac or to a private network is refused.
  read_chrome(url)  a page in Zeus's own Chrome (the separate Chrome with its own data folder that carries the owner's
                    sign-ins, reachable on a local control port). A NEW background tab is opened at the address, its
                    text is read once the page has settled, and that tab is closed again. A tab that is already
                    open is never reused or reloaded: its text may be old, a job of Zeus's may be in the middle of
                    using it, and reloading a page that came from a submitted form would submit the form again.

Both answer with the same small dictionary and never raise:
  {'ok': True,  'url': asked, 'final_url': where it ended up, 'title': ..., 'text': the page as plain lines}
  {'ok': False, 'url': asked, 'why': one plain line}
"Could not read" is the caller's `unknown`. It is never an empty page.

Which Python: this file uses the standard library only, so it runs under the engine's own interpreter
(python3 -I), like tick.py. Chrome's control port speaks a websocket and Python has no websocket client built in.
Hermes's own helper for this (tools/browser_cdp_tool.py, _cdp_call) uses the third-party `websockets` package from
the Hermes virtual-env, and importing it also loads Hermes's tool registry and config. The engine does not load
third-party packages, so the few lines of websocket needed (one handshake, small text frames, to this Mac only)
are written out here instead.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

CDP = os.environ.get('LIFE_CHROME_CDP', 'http://127.0.0.1:9333')   # Zeus's own Chrome (zeus-spine-20261111/agents-chrome)
MAX_BYTES = 2_000_000          # of a public page; more than this is cut, never refused (the piece that matters is near the top or it is not a page to watch)
MAX_TEXT = 200_000             # characters of page text handed back
WEB_TIMEOUT = 15
CHROME_TIMEOUT = 25
SETTLE_POLL = 0.5              # seconds between looks while a page in Chrome is still loading
MAX_REDIRECTS = 5
AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
TEXT_TYPES = ('text/', 'application/json', 'application/xhtml+xml', 'application/xml', 'application/rss+xml', 'application/atom+xml')

# The only things this file ever asks Chrome to do, and the only script it ever runs in a page. The script reads
# four values and changes nothing. No caller can pass another method or another script (see _Chrome.call).
CHROME_METHODS = frozenset(('Target.createTarget', 'Target.attachToTarget', 'Runtime.evaluate', 'Target.closeTarget'))
PAGE_SCRIPT = ("JSON.stringify({ready: document.readyState, href: location.href, title: document.title, "
               "text: document.body ? document.body.innerText.slice(0, %d) : ''})" % MAX_TEXT)


def _fail(url, why) -> dict:
    return {'ok': False, 'url': url, 'why': ' '.join(str(why).split())[:200]}


# ---- a public page, by GET ---------------------------------------------------------------------------------------------

class _Text(HTMLParser):
    """HTML -> the lines a person would read. Scripts, styles and hidden templates are left out."""
    SKIP = frozenset(('script', 'style', 'noscript', 'template', 'svg'))
    BREAK = frozenset(('p', 'div', 'br', 'li', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'header', 'footer',
                       'table', 'ul', 'ol', 'dl', 'dt', 'dd', 'form', 'label', 'button', 'option', 'nav', 'main', 'aside', 'pre', 'blockquote', 'hr'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.title, self._skip, self._in_title = [], '', 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == 'title':
            self._in_title = True
        if tag in self.BREAK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == 'title':
            self._in_title = False
        if tag in self.BREAK:
            self.out.append('\n')

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.out.append(data)


def html_text(html: str) -> tuple:
    """-> (title, text). Never raises on broken HTML: what was read up to the break is kept."""
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    lines = [' '.join(ln.split()) for ln in ''.join(p.out).splitlines()]
    return ' '.join(p.title.split())[:300], '\n'.join(ln for ln in lines if ln)[:MAX_TEXT]


def public_host(host: str) -> str:
    """'' when this name resolves only to addresses out on the public internet, else why not. A name that points at
    this Mac, the home network or a link-local address is how a "public page" is turned into a look inside."""
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as e:
        return f'the name could not be looked up ({type(e).__name__})'
    if not found:
        return 'the name has no address'
    for ip in found:
        try:
            if not ipaddress.ip_address(ip.split('%')[0]).is_global:
                return 'the name points at a private or local address'
        except ValueError:
            return 'the name has an address that cannot be read'
    return ''


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only as another plain GET to an address that passes the same rule as the first one."""
    max_redirections = MAX_REDIRECTS

    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = self.allowed(newurl)
        if problem:
            raise urllib.error.URLError(f'the page sent the read on to an address that is not allowed ({problem})')
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (new.get_method() != 'GET' or new.data is not None):
            raise urllib.error.URLError('the page asked for something other than a plain read')
        return new


def read_web(url: str, allowed=None, timeout: float = WEB_TIMEOUT, resolve=public_host, keep_raw: bool = False) -> dict:
    """One plain GET of a public page. `allowed(url) -> ''` or why not (watches.address_problem); it is asked for the
    first address and for every redirect. `resolve` is the name check, a stand-in in tests."""
    allowed = allowed or (lambda _u: '')

    def gate(u):
        problem = allowed(u)
        if problem:
            return problem
        return resolve(urllib.parse.urlsplit(u).hostname or '')

    problem = gate(url)
    if problem:
        return _fail(url, problem)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _CheckedRedirects(gate))   # no stored logins, no site data, no proxy
    req = urllib.request.Request(url, method='GET', headers={'User-Agent': AGENT, 'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8',
                                                              'Accept-Language': 'en-US,en;q=0.8'})
    try:
        with opener.open(req, timeout=timeout) as r:
            kind = str(r.headers.get_content_type() or '').lower()
            if not kind.startswith(TEXT_TYPES):
                return _fail(url, f'the address is not a page of text ({kind or "unknown kind"})')
            raw = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset() or 'utf-8'
            final = r.geturl()
    except urllib.error.HTTPError as e:
        e.close()
        return _fail(url, f'the site answered with an error ({e.code})')
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _fail(url, f'the page could not be read ({getattr(e, "reason", None) or type(e).__name__})')
    try:
        body = raw.decode(charset, 'replace')
    except LookupError:
        body = raw.decode('utf-8', 'replace')
    if 'html' in kind or 'xml' in kind:
        title, text = html_text(body)
    else:
        title, text = '', '\n'.join(ln.rstrip() for ln in body.splitlines() if ln.strip())[:MAX_TEXT]
    return {'ok': True, 'url': url, 'final_url': final, 'title': title, 'text': text} | ({'raw': body} if keep_raw else {})   # keep_raw (researchflight.py): the page as it came, for a data block the text view drops


# ---- a page in Zeus's own Chrome ---------------------------------------------------------------------------------------

def _local(base: str) -> tuple:
    """The control port must be on this Mac. -> (host, port) or raises ValueError."""
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ('http', 'ws') or u.hostname not in ('127.0.0.1', 'localhost', '::1') or not u.port:
        raise ValueError('the Chrome control address is not on this Mac')
    return u.hostname, u.port


class _Socket:
    """The small part of a websocket this file needs: one handshake, text messages out (masked, as a client must),
    text messages in (whole or in pieces), a pong for a ping. To this Mac only."""

    def __init__(self, ws_url: str, timeout: float):
        u = urllib.parse.urlsplit(ws_url)
        host, port = _local(ws_url)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b''
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (('?' + u.query) if u.query else '')
        # No Origin line: Chrome's control port turns away a caller that names a web page as its origin.
        self.sock.sendall((f'GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            more = self.sock.recv(4096)
            if not more:
                raise ConnectionError('Chrome closed the line during the handshake')
            head += more
            if len(head) > 65536:
                raise ConnectionError('Chrome answered the handshake with too much')
        head, _, self.buf = head.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        want = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        got = {ln.split(':', 1)[0].strip().lower(): ln.split(':', 1)[1].strip() for ln in lines[1:] if ':' in ln}
        if ' 101 ' not in lines[0] + ' ' or got.get('sec-websocket-accept') != want:
            raise ConnectionError('Chrome did not accept the connection')

    def settimeout(self, seconds: float):
        self.sock.settimeout(max(0.05, seconds))

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            more = self.sock.recv(65536)
            if not more:
                raise ConnectionError('Chrome closed the line')
            self.buf += more
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536
                                         else bytes([0x80 | 127]) + struct.pack('>Q', n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text: str):
        self._frame(0x1, text.encode())

    def recv(self) -> str:
        """The next whole text message."""
        parts = []
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._take(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._take(8))[0]
            if n > 16_000_000:
                raise ConnectionError('Chrome sent a message that is too large')
            mask = self._take(4) if b1 & 0x80 else b''
            data = self._take(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 0x8:
                raise ConnectionError('Chrome closed the line')
            if op == 0x9:
                self._frame(0xA, data)       # a ping: answer it and keep reading
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if b0 & 0x80:
                return b''.join(parts).decode('utf-8', 'replace')

    def close(self):
        try:
            self._frame(0x8, b'')
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _Chrome:
    def __init__(self, ws_url: str, deadline: float):
        self.deadline = deadline
        self.ws = _Socket(ws_url, max(1.0, deadline - time.monotonic()))
        self.n = 0

    def call(self, method: str, params: dict, session: str = '') -> dict:
        """One command. Anything outside the four this file needs is refused here, whatever the caller meant."""
        if method not in CHROME_METHODS or (method == 'Runtime.evaluate' and params.get('expression') != PAGE_SCRIPT):
            raise PermissionError('not a read')
        self.n += 1
        mine = self.n
        self.ws.send(json.dumps({'id': mine, 'method': method, 'params': params, **({'sessionId': session} if session else {})}))
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('Chrome took too long')
            self.ws.settimeout(left)
            msg = json.loads(self.ws.recv())
            if msg.get('id') == mine:           # everything else on the line is Chrome's own news about other tabs
                if 'error' in msg:
                    raise RuntimeError(str((msg['error'] or {}).get('message') or 'Chrome refused')[:120])
                return msg.get('result') or {}


def read_chrome(url: str, allowed=None, timeout: float = CHROME_TIMEOUT, cdp: str | None = None) -> dict:
    """The text of one page in Zeus's own Chrome: a new background tab at `url`, read when settled, then closed.
    `allowed(url) -> ''` or why not; asked before anything is opened. Where the page ended up is handed back as
    'final_url' and is not judged here: landing on a sign-in page is exactly what the signed-in check looks for."""
    problem = (allowed or (lambda _u: ''))(url)
    if problem:
        return _fail(url, problem)
    base = (cdp or CDP).rstrip('/')
    deadline = time.monotonic() + timeout
    chrome = tab = None
    try:
        host, port = _local(base)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(f'http://{host}:{port}/json/version', method='GET'), timeout=min(5, timeout)) as r:
            ws_url = str(json.loads(r.read(200_000)).get('webSocketDebuggerUrl') or '')
        if _local(ws_url)[1] != port:
            raise ValueError('Chrome named a control line that is not its own')
        chrome = _Chrome(ws_url, deadline)
        tab = chrome.call('Target.createTarget', {'url': url, 'background': True}).get('targetId')
        if not tab:
            raise RuntimeError('Chrome did not open a tab')
        session = chrome.call('Target.attachToTarget', {'targetId': tab, 'flatten': True}).get('sessionId')
        if not session:
            raise RuntimeError('Chrome did not let the tab be read')
        seen, last, steady = None, -1, 0
        while time.monotonic() < deadline - 1.0:
            got = chrome.call('Runtime.evaluate', {'expression': PAGE_SCRIPT, 'returnByValue': True}, session)
            try:
                seen = json.loads((got.get('result') or {}).get('value') or '{}')
            except ValueError:
                seen = None
            size = len((seen or {}).get('text') or '')
            # Settled: the page says it finished loading and its text was the same length on two looks in a row
            # (many account pages fill in after "finished").
            steady = steady + 1 if (seen or {}).get('ready') == 'complete' and size == last and size > 0 else 0
            if steady >= 2:
                break
            last = size
            time.sleep(SETTLE_POLL)
        if not seen or seen.get('ready') != 'complete' or not str(seen.get('href') or '').startswith(('http://', 'https://')):
            return _fail(url, 'the page did not finish loading in time')
        text = '\n'.join(' '.join(ln.split()) for ln in str(seen.get('text') or '').splitlines() if ln.strip())[:MAX_TEXT]
        return {'ok': True, 'url': url, 'final_url': str(seen.get('href')), 'title': ' '.join(str(seen.get('title') or '').split())[:300], 'text': text}
    except (urllib.error.URLError, OSError, ValueError, RuntimeError, TimeoutError, PermissionError, KeyError) as e:
        why = getattr(e, 'reason', None) or e
        return _fail(url, f"Zeus's own Chrome could not be reached or did not answer ({type(e).__name__}: {str(why)[:80]})")
    finally:
        if chrome is not None:
            if tab:
                try:
                    chrome.deadline = max(chrome.deadline, time.monotonic() + 3)    # the tab this read opened is always closed again
                    chrome.call('Target.closeTarget', {'targetId': tab})
                except Exception:
                    pass
            chrome.ws.close()
