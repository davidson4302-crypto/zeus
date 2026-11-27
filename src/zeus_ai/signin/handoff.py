"""handoff: Zeus phone sign-in handoff, the shared parts.

When one of the owner's store sign-ins lapses in Zeus's own Chrome, Zeus's ask can carry a link. The link opens a small
page on the owner's phone that shows ONE tab of Zeus's Chrome (the store's sign-in page) and lets them tap and type into it.
A "Done" button makes the life engine's own sign-in check look again right away.

This file is the part both the server (handoff_server.py) and Zeus's side use:

  * the site table (handoff-sites.json here, plus the engine's signin-sites.json for the account page)
  * the guards: which pages may be typed into, which pages the tab may go to, which buttons may never be pressed
    (the purchase lock's store table, read from its own stores.json)
  * the session file <life>/signin-handoff.json and the event log <life>/signin-handoff.log.jsonl
  * a small Chrome control-port client (stdlib only; Hermes's python has no websocket package)
  * the engine call: signedin.check(site, life, force=True), the engine imported read-only
  * the words: the ask line Zeus sends, the page's Done answers, the sms: link

CLI (Hermes python, -I is fine):
  handoff.py open <site>           new session (replaces an open one), prints link, sms link, expiry
  handoff.py link <site> [--json]  the line Zeus should send; reuses an open session, else opens one
  handoff.py close <site>          ends the session and closes the tab it opened
  handoff.py status                sessions without their tokens

Environment:
  LIFE_HOME            the life folder (default: settings.LIFE_HOME)
  HANDOFF_CDP          Chrome control address. Default: config signin.cdp_url (http://127.0.0.1:9333) ONLY when
                       LIFE_HOME is the live folder; any other life folder must name its own Chrome, and the live
                       Chrome's port is refused for it.
  HANDOFF_SITES        site table (default: config signin.sites_file, else handoff-sites.json beside this file)
  HANDOFF_STORES       purchase lock store table (default: plugins/purchase-lock/stores.json)
  HANDOFF_ENGINE_DIR   life engine folder (default: settings.ENGINE_DIR)
  HANDOFF_PUBLIC_BASE  public link base (default: config signin.public_base; required to send links, e.g. an
                       HTTPS reverse proxy or tunnel in front of handoff_server.py)

Never logged or written anywhere: what is typed. The token is only in the session file (mode 0600) and the link.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'engine'))
import settings  # noqa: E402

PACIFIC = ZoneInfo(settings.OWNER_TZ)                 # the owner's clock (name kept for callers)
LIVE_LIFE = Path(settings.LIFE_HOME)
LIVE_CDP = str(settings.get('signin.cdp_url', 'http://127.0.0.1:9333')).rstrip('/')
ENGINE_DIR = Path(os.environ.get('HANDOFF_ENGINE_DIR', str(settings.ENGINE_DIR)))
STORES_JSON = Path(os.environ.get('HANDOFF_STORES', str(HERE.parent / 'plugins' / 'purchase-lock' / 'stores.json')))
SITES_JSON = Path(os.environ.get('HANDOFF_SITES') or settings.get('signin.sites_file') or str(HERE / 'handoff-sites.json'))
PUBLIC_BASE = str(os.environ.get('HANDOFF_PUBLIC_BASE') or settings.get('signin.public_base', '') or '').rstrip('/')
AGENT_NUMBER = str(settings.get('agent.phone', '') or '')   # the agent's own texting handle, for the sms: link
SESSION_FILE = 'signin-handoff.json'
LOG_FILE = 'signin-handoff.log.jsonl'
TTL = 2 * 3600
TOKEN_RX = re.compile(r'[A-Za-z0-9_-]{43}')        # 32 random bytes, base64url without padding

# Hosts whose boxes (in a frame from another site) may be tapped on a sign-in page: the usual "are you a robot" checks.
CHALLENGE_HOSTS = ('google.com', 'recaptcha.net', 'gstatic.com', 'hcaptcha.com', 'challenges.cloudflare.com', 'arkoselabs.com',
                   'funcaptcha.com', 'px-cdn.net', 'px-cloud.net', 'perimeterx.net', 'captcha-delivery.com', 'amazon.com')

# Used only when the purchase lock's stores.json cannot be read: a copy of its generic section, so the
# button check never runs empty.
FALLBACK_GENERIC = {
    'commit_strong': [
        "(?:place|submit|complete|confirm|finish)(?: and pay)? (?:your |my |this |the )?(?:order|orders|purchase)(?: now| securely| and pay)?",
        "buy (?:it )?now", "pay now", "pay securely", "confirm (?:and|&) pay", "complete (?:your )?(?:purchase|payment|checkout|booking)",
        "confirm (?:your )?(?:purchase|payment|booking)", "place order and pay", "submit payment"],
    'commit_weak': ["(?:buy|purchase|pay|order|book)(?: it| this| now)?", "(?:complete|confirm) (?:order|booking)", "book (?:flight|flights|trip)"],
    'commit_attr': "place.?(?:your.?)?order|submit.?order|buy.?now|pay.?now|complete.?purchase|checkout.?submit|purchase.?button|confirm.?purchase",
    'commit_url': ["/checkout", "/purchase", "/payment", "/buy/", "/order/review", "/place-?order"],
}

# A path piece (split on / and on - _ .) that is one of these words: the tab is never sent there.
NAV_DENY_WORDS = frozenset(('cart', 'basket', 'checkout', 'payment', 'payments', 'pay', 'purchase', 'buy', 'buynow', 'placeorder',
                            'addtocart', 'orderreview', 'reviewandpay'))
# Anywhere in the path (lower case, - _ . removed): view only, nothing is typed or tapped. Wider than the list above,
# so an orders or billing page can be seen (where a sign-in may land) but never used.
INPUT_DENY_PARTS = ('cart', 'basket', 'checkout', 'payment', 'pay', 'purchase', 'buy', 'order', 'billing', 'wallet', 'giftcard',
                    'subscri', 'booking', 'reorder', 'redeem', 'transfer')


# Buttons that are not a purchase but change a cart, a list or an order (the purchase lock does not count them; on a
# sign-in errand nothing like this is ever needed). Matched whole against the normalised label, like the lock's.
EXTRA_REFUSE = [re.compile(p) for p in (
    r'add(?: (?:it|this|all|items?|to))*(?: to)? (?:cart|bag|basket|trolley|order|list|registry)', r'buy (?:it )?again', r'reorder(?: all| items?)?',
    r'subscribe(?: (?:and|&) save)?', r'start (?:a )?subscription', r'add', r'checkout', r'check out', r'proceed to checkout',
    r'continue to checkout', r'go to (?:cart|checkout)', r'view cart', r'start checkout')]


def _now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(), PACIFIC).isoformat(timespec='seconds')


def clock(ts: float) -> str:
    """'8:42pm' on the owner's clock."""
    t = datetime.fromtimestamp(ts, PACIFIC)
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


# ---- where things are -------------------------------------------------------------------------------------------------

def life_home() -> Path:
    return Path(os.environ.get('LIFE_HOME') or str(LIVE_LIFE))


def is_live(life: Path) -> bool:
    try:
        return Path(life).resolve() == LIVE_LIFE.resolve()
    except OSError:
        return False


def cdp_base(life: Path | None = None) -> str:
    """The Chrome this handoff shows and checks. A sandbox can never fall through to Zeus's real Chrome."""
    life = Path(life or life_home())
    given = (os.environ.get('HANDOFF_CDP') or '').strip().rstrip('/')
    if not given:
        if is_live(life):
            return LIVE_CDP
        raise SystemExit('HANDOFF_CDP must be set when LIFE_HOME is not the live life folder')
    host, port = local_hostport(given)
    if not is_live(life) and port == local_hostport(LIVE_CDP)[1]:
        raise SystemExit(f"port {port} is Zeus's real Chrome; a sandbox life folder may not use it")
    return f'http://{host}:{port}'


def local_hostport(base: str) -> tuple:
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ('http', 'ws') or u.hostname not in ('127.0.0.1', 'localhost') or not u.port:
        raise ValueError('the Chrome control address must be http://127.0.0.1:<port>')
    return u.hostname, u.port


# ---- the engine, imported read-only -------------------------------------------------------------------------------------

_engine_lock = threading.Lock()


def engine():
    """(signedin, pageread) from the life engine folder. Never writes bytecode there; appended to the path, so the
    standard library always wins a name clash."""
    with _engine_lock:
        sys.dont_write_bytecode = True
        if str(ENGINE_DIR) not in sys.path:
            sys.path.append(str(ENGINE_DIR))
        import pageread  # noqa: E402
        import signedin  # noqa: E402
        return signedin, pageread


def check_now(site: str, life: Path, cdp: str) -> tuple:
    """Ask the engine to look now: signedin.check(site, life, force=True), reading the account page in the same Chrome
    this handoff shows. -> (status, usable). usable=False: the site table entry is still a placeholder, so the
    engine cannot tell and answers 'unknown' without reading anything."""
    signedin, pageread = engine()
    entry = signedin.sites(life).get(site)
    if entry is None or not signedin.usable(entry):
        return 'unknown', False
    use = {'chrome': lambda url, allowed=None: pageread.read_chrome(url, allowed, cdp=cdp),
           'web': lambda url, allowed=None: {'ok': False, 'url': url, 'why': 'not used by the sign-in handoff'}}
    try:
        return str(signedin.check(site, life, force=True, use=use)), True
    except Exception as e:                                   # the engine never stops the page
        log(life, 'check_error', site=site, error=type(e).__name__)
        return 'unknown', True


# ---- the site table ---------------------------------------------------------------------------------------------------

def site_table(life: Path | None = None) -> dict:
    """{site: {id, name, hosts, start_url, store}}. Hosts and the fallback start page come from handoff-sites.json;
    the engine's signin-sites.json (life copy first) supplies the account page once it is filled in, and the name."""
    life = Path(life or life_home())
    try:
        raw = json.loads(SITES_JSON.read_text())
    except (OSError, ValueError):
        raw = {}
    table = {}
    for sid, s in (raw.get('sites') or {}).items():
        if isinstance(s, dict) and s.get('hosts'):
            table[str(sid)] = {'id': str(sid), 'name': str(s.get('name') or sid), 'hosts': [str(h).lower() for h in s['hosts']],
                               'start_url': str(s.get('start_url') or ''), 'store': s.get('store')}
    try:
        signedin, _ = engine()
        eng = signedin.sites(life)
        for sid, e in eng.items():
            if sid in table:
                if e.get('name'):
                    table[sid]['name'] = str(e['name'])
                if signedin.usable(e):
                    table[sid]['start_url'] = str(e['account_url'])
    except Exception:
        pass
    return table


# ---- the guards -------------------------------------------------------------------------------------------------------

def norm(label) -> str:
    """Button words, normalised exactly as the purchase lock does (buyfence.norm): 'Place order  $42.17' -> 'place order'."""
    s = str(label or '').lower().replace('&', ' & ').replace('’', "'")
    s = re.sub(r'\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?', ' ', s)
    s = re.sub(r"[^a-z&' ]+", ' ', s).replace("'", '')
    return re.sub(r'\s+', ' ', s).strip()


def host_in(host: str, hosts) -> bool:
    host = str(host or '').lower().rstrip('.')
    return bool(host) and any(host == h or host.endswith('.' + h) for h in hosts or [])


class Rules:
    """What may happen in the tab. Built from the purchase lock's stores.json (patterns reused, not copied)."""

    def __init__(self, stores_path: Path | None = None):
        path = Path(stores_path or STORES_JSON)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            g = data['generic']
            self.source = str(path)
        except (OSError, ValueError, KeyError):
            data, g, self.source = {'stores': {}}, FALLBACK_GENERIC, 'built-in copy'
        self.strong = [re.compile(p, re.I) for p in g['commit_strong']]
        self.weak = [re.compile(p, re.I) for p in g['commit_weak']]
        self.attr_rx = re.compile(g['commit_attr'], re.I)
        self.generic_urls = [re.compile(p, re.I) for p in g['commit_url']]
        self.store_urls = {sid: [re.compile(p, re.I) for p in (s.get('commit_url') or [])] for sid, s in (data.get('stores') or {}).items()}

    # buttons
    def commit_label(self, labels, attrs='') -> bool:
        """A purchase-lock commit button: a strong OR weak label matched whole, or a commit attribute. Weak labels count
        everywhere here (the purchase lock counts them only on a checkout page): a sign-in page has no Buy button."""
        names = [n for n in map(norm, labels or []) if n]
        if any(rx.fullmatch(n) for n in names for rx in self.strong + self.weak):
            return True
        return bool(attrs) and bool(self.attr_rx.search(str(attrs)))

    # pages
    def commit_path(self, site: dict, path: str) -> bool:
        path = path or '/'
        rules = self.generic_urls + self.store_urls.get(str(site.get('store') or ''), [])
        if any(rx.search(path) for rx in rules):
            return True
        for piece in path.lower().split('/'):
            words = {re.sub(r'[-_.]', '', piece), *re.split(r'[-_.]+', piece)}
            if words & NAV_DENY_WORDS:
                return True
        return False

    def nav_ok(self, site: dict, url: str) -> tuple:
        """May the tab GO to this address? Only https pages on the site's own hosts that are not checkout-type pages."""
        try:
            u = urllib.parse.urlsplit(str(url or ''))
        except ValueError:
            return False, 'leaves'
        if u.scheme in ('about', 'data', 'blob', 'chrome-error') or str(url).startswith('about:'):
            return True, ''                                   # no network, nothing leaves (blank and error pages)
        if u.scheme != 'https' or not host_in(u.hostname, site.get('hosts')):
            return False, 'leaves'
        if self.commit_path(site, u.path):
            return False, 'commit_page'
        return True, ''

    def input_ok(self, site: dict, url: str) -> tuple:
        """May taps and typing reach the tab now? The site's own hosts, and a path with nothing about orders, money,
        carts or checkout in it."""
        ok, why = self.nav_ok(site, url)
        u = urllib.parse.urlsplit(str(url or ''))
        if not ok or u.scheme != 'https':
            return False, 'view_only'
        squashed = re.sub(r'[-_.]', '', (u.path or '/').lower())
        if any(p in squashed for p in INPUT_DENY_PARTS):
            return False, 'view_only'
        return True, ''

    def frame_ok(self, site: dict, src: str) -> bool:
        host = urllib.parse.urlsplit(str(src or '')).hostname or ''
        return host_in(host, site.get('hosts')) or host_in(host, CHALLENGE_HOSTS)

    def tap_refusal(self, site: dict, probe: dict) -> str:
        """'' = the tap may go ahead; else a note code. probe: what is under the finger (handoff_server.PROBE)."""
        hit = (probe or {}).get('hit')
        if not hit:
            return ''
        for fr in hit.get('frames') or []:
            if not fr.get('same') and not self.frame_ok(site, fr.get('src')):
                return 'frame'
        c, s = hit.get('clickable') or {}, hit.get('self') or {}
        labels = list(c.get('labels') or []) + list(s.get('labels') or [])
        if self.commit_label(labels, f"{c.get('attrs') or ''} {s.get('attrs') or ''}"):
            return 'commit'
        if any(rx.fullmatch(n) for n in map(norm, labels) if n for rx in EXTRA_REFUSE):
            return 'commit'
        for d in (c, s):
            href = str(d.get('href') or '')
            if href and not href.lower().startswith('javascript:'):
                ok, why = self.nav_ok(site, href)
                if not ok:
                    return 'leaves' if why == 'leaves' else 'commit_page'
            action = str(d.get('formaction') or '')
            if action and not self.nav_ok(site, action)[0]:
                return 'commit'
        return ''

    def enter_refusal(self, site: dict, probe: dict) -> str:
        """Enter presses whatever has focus, or sends the form it is in through its submit button."""
        for fr in (probe or {}).get('focus_frames') or []:
            if not fr.get('same') and not self.frame_ok(site, fr.get('src')):
                return 'frame'
        f = (probe or {}).get('focus') or {}
        if f and self.commit_label(f.get('labels'), f.get('attrs')) and f.get('tag') in ('button', 'a', 'input') and \
                (f.get('tag') != 'input' or f.get('type') in ('submit', 'button', 'image')):
            return 'commit'
        if f.get('href') and not self.nav_ok(site, f['href'])[0]:
            return 'leaves'
        for b in (probe or {}).get('form') or []:
            if self.commit_label(b.get('labels'), b.get('attrs')):
                return 'commit'
        action = str((probe or {}).get('form_action') or '')
        if action and not self.nav_ok(site, action)[0]:
            return 'commit'
        return ''


def can_type(probe: dict, rules: Rules, site: dict) -> str:
    """'' when the focused thing takes text (a text box, or a box inside an allowed frame), else a note code."""
    for fr in (probe or {}).get('focus_frames') or []:
        if not fr.get('same'):
            return '' if rules.frame_ok(site, fr.get('src')) else 'frame'
    f = (probe or {}).get('focus') or {}
    if f.get('editable') or f.get('tag') == 'textarea':
        return ''
    if f.get('tag') == 'input' and f.get('type') in ('', 'text', 'email', 'tel', 'number', 'password', 'search', 'url'):
        return ''
    return 'tap_box'


# ---- the session file -------------------------------------------------------------------------------------------------

class Sessions:
    """<life>/signin-handoff.json: {'version': 1, 'sessions': {site: {...}}}. One session per site. Read, changed and
    written under one file lock (the CLI and the server both write), then moved over the real file, mode 0600."""

    def __init__(self, life: Path, name: str = SESSION_FILE):
        """name: another session file in the same shape (e.g. knows-page.json, the "what Zeus knows" page)."""
        self.life = Path(life)
        self.name = name
        self.path = self.life / name

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get('sessions'), dict):
                return d
        except (OSError, ValueError):
            pass
        return {'version': 1, 'sessions': {}}

    def change(self, fn):
        self.life.mkdir(parents=True, exist_ok=True)
        with open(self.life / (self.name + '.lock'), 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            d = self.load()
            out = fn(d)
            tmp = self.life / f'.{self.name}.{os.getpid()}.{threading.get_ident()}.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, 'w') as fh:
                    fh.write(json.dumps(d, indent=1))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return out

    def find(self, token: str) -> dict | None:
        """The open, unexpired session this token belongs to, compared in constant time; else None."""
        if not isinstance(token, str) or not TOKEN_RX.fullmatch(token):
            return None
        found = None
        for s in self.load()['sessions'].values():
            t = s.get('token') or ''
            if t and hmac.compare_digest(t.encode(), token.encode()) and s.get('state') == 'open' and float(s.get('expires_ts') or 0) > _now():
                found = dict(s)
        return found

    def get(self, site: str) -> dict | None:
        s = self.load()['sessions'].get(site)
        return dict(s) if s else None

    def close(self, site: str, state: str, why: str, token: str | None = None) -> dict | None:
        """End a session (its token is wiped). token: only if it is still this session (no race with a newer open)."""
        def end(d):
            s = d['sessions'].get(site)
            if not s or s.get('state') != 'open' or (token and s.get('token') != token):
                return None
            before = dict(s)
            s.update(state=state, token=None, closed_at=iso(), closed_why=why)
            return before
        return self.change(end)


def public_id(token: str) -> str:
    """A short name for a session in logs: never the token."""
    return hashlib.sha256(token.encode()).hexdigest()[:10]


def link_for(token: str) -> str:
    if not PUBLIC_BASE:
        raise SystemExit('no public link base: set signin.public_base in the config (or HANDOFF_PUBLIC_BASE)')
    return f'{PUBLIC_BASE}/{token}'


# ---- the log ----------------------------------------------------------------------------------------------------------

_log_lock = threading.Lock()


def log(life: Path, event: str, **fields):
    """One line per session event. Callers pass counts and codes only: never typed text, never a token, never a
    full address (a host at most)."""
    row = {'at': iso(), 'event': event, **{k: v for k, v in fields.items() if v is not None}}
    line = json.dumps(row, ensure_ascii=False, default=str) + '\n'
    try:
        Path(life).mkdir(parents=True, exist_ok=True)
        with _log_lock:
            fd = os.open(Path(life) / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as fh:
                fh.write(line)
    except OSError:
        pass


# ---- the words --------------------------------------------------------------------------------------------------------

def ask_line(name: str, link: str) -> str:
    return f'{name} signed me out. You can sign me back in from your phone here: {link} (works for 2 hours)'


def sms_text(name: str) -> str:
    return f'Done, signed in to {name}'


def sms_link(name: str) -> str:
    """iPhone form: sms:<number>&body=<text>. Empty when agent.phone is not configured (the page hides the button)."""
    if not AGENT_NUMBER:
        return ''
    return f'sms:{AGENT_NUMBER}&body=' + urllib.parse.quote(sms_text(name), safe='')


def done_words(status: str, name: str, usable: bool = True) -> str:
    if status == 'signed_in':
        return f'{name} is signed in. Zeus will pick the errand back up.'
    if status == 'signed_out':
        return 'Still shows the sign-in page. Try again or tap Done when finished.'
    if not usable:
        return f"Zeus can't check {name} by itself yet. If you finished signing in, text Zeus \"{sms_text(name)}\"."
    return f"Couldn't tell yet if {name} is signed in. Wait a few seconds and tap Done again."


# ---- Chrome's control port --------------------------------------------------------------------------------------------

class CDPError(RuntimeError):
    pass


# Everything this handoff may ask Chrome to do. Nothing reads cookies, storage or other tabs' contents.
ALLOWED_METHODS = frozenset((
    'Target.getTargets', 'Target.getTargetInfo', 'Target.createTarget', 'Target.attachToTarget', 'Target.detachFromTarget',
    'Target.closeTarget', 'Target.setDiscoverTargets',
    'Page.enable', 'Page.getFrameTree', 'Page.navigate', 'Page.getNavigationHistory', 'Page.navigateToHistoryEntry',
    'Page.captureScreenshot', 'Page.bringToFront',
    'Page.createIsolatedWorld', 'Page.handleJavaScriptDialog',
    'Runtime.evaluate',
    'Input.dispatchMouseEvent', 'Input.dispatchKeyEvent', 'Input.insertText',
    'Emulation.setDeviceMetricsOverride', 'Emulation.clearDeviceMetricsOverride', 'Emulation.setFocusEmulationEnabled',
    'Fetch.enable', 'Fetch.disable', 'Fetch.continueRequest', 'Fetch.failRequest',
))
PROBE_MARK = '/*zeus-handoff probe*/'


class CDP:
    """A websocket to Chrome's browser endpoint on this Mac: commands from any thread, events to one callback on a
    reader thread. Only the methods above; Runtime.evaluate only our own read-only probe, in an isolated world."""

    def __init__(self, base: str, timeout: float = 8.0, on_event=None):
        host, port = local_hostport(base)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://{host}:{port}/json/version', timeout=timeout) as r:
            ws_url = str(json.loads(r.read(200_000)).get('webSocketDebuggerUrl') or '')
        wu = urllib.parse.urlsplit(ws_url)
        if wu.hostname not in ('127.0.0.1', 'localhost') or wu.port != port:
            raise CDPError('Chrome named a control line that is not its own')
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {wu.path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            more = self.sock.recv(4096)
            if not more:
                raise CDPError('Chrome closed the line during the handshake')
            head += more
        head, _, self.buf = head.partition(b'\r\n\r\n')
        want = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if b' 101 ' not in head.split(b'\r\n')[0] + b' ' or want.encode() not in head:
            raise CDPError('Chrome did not accept the connection')
        self.sock.settimeout(None)
        self.on_event = on_event
        self.n = 0
        self.pending = {}
        self.wlock = threading.Lock()
        self.plock = threading.Lock()
        self.alive = True
        self.reader = threading.Thread(target=self._read_loop, name='cdp-reader', daemon=True)
        self.reader.start()

    # wire
    def _frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('>H', n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('>Q', n)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self.wlock:
            self.sock.sendall(head + mask + masked)

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            more = self.sock.recv(262144)
            if not more:
                raise ConnectionError('Chrome closed the line')
            self.buf += more
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._take(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._take(8))[0]
            if n > 64_000_000:
                raise ConnectionError('message too large')
            mask = self._take(4) if b1 & 0x80 else b''
            data = self._take(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 0x8:
                raise ConnectionError('Chrome closed the line')
            if op == 0x9:
                self._frame(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if b0 & 0x80:
                return b''.join(parts).decode('utf-8', 'replace')

    def _read_loop(self):
        try:
            while self.alive:
                msg = json.loads(self._recv())
                if 'id' in msg:
                    with self.plock:
                        slot = self.pending.pop(msg['id'], None)
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
                elif self.on_event is not None:
                    try:
                        self.on_event(msg.get('method'), msg.get('params') or {}, msg.get('sessionId') or '')
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.alive = False
            with self.plock:
                waiting, self.pending = list(self.pending.values()), {}
            for slot in waiting:
                slot[1] = {'error': {'message': 'Chrome line closed'}}
                slot[0].set()
            if self.on_event is not None:
                try:
                    self.on_event('_closed', {}, '')
                except Exception:
                    pass

    # commands
    def _check(self, method: str, params: dict):
        if method not in ALLOWED_METHODS:
            raise PermissionError(f'{method} is not something the sign-in handoff does')
        if method == 'Runtime.evaluate' and (not str(params.get('expression') or '').startswith(PROBE_MARK) or 'contextId' not in params):
            raise PermissionError('only the read-only probe runs in a page, and only in its own isolated world')

    def call(self, method: str, params: dict | None = None, session: str = '', timeout: float = 10.0) -> dict:
        params = params or {}
        self._check(method, params)
        if not self.alive:
            raise CDPError('Chrome line closed')
        with self.plock:
            self.n += 1
            mid = self.n
            slot = [threading.Event(), None]
            self.pending[mid] = slot
        self._frame(0x1, json.dumps({'id': mid, 'method': method, 'params': params, **({'sessionId': session} if session else {})}).encode())
        if not slot[0].wait(timeout):
            with self.plock:
                self.pending.pop(mid, None)
            raise CDPError(f'{method} took too long')
        msg = slot[1] or {}
        if 'error' in msg:
            raise CDPError(str((msg['error'] or {}).get('message') or 'Chrome refused')[:160])
        return msg.get('result') or {}

    def send(self, method: str, params: dict | None = None, session: str = ''):
        """Fire and forget (for the reader thread, which must never wait on itself)."""
        params = params or {}
        self._check(method, params)
        if not self.alive:
            return
        with self.plock:
            self.n += 1
            mid = self.n
        try:
            self._frame(0x1, json.dumps({'id': mid, 'method': method, 'params': params, **({'sessionId': session} if session else {})}).encode())
        except OSError:
            pass

    def close(self):
        self.alive = False
        try:
            self._frame(0x8, b'')
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def page_targets(cdp: CDP) -> dict:
    return {t['targetId']: t for t in cdp.call('Target.getTargets').get('targetInfos', []) if t.get('type') == 'page'}


def ensure_tab(base: str, site: dict, target_id: str | None) -> tuple:
    """-> (target_id, created). Reuses only the tab this handoff opened before (a tab Zeus's own jobs opened is never
    touched); otherwise opens a new background tab on the site's account/sign-in page."""
    c = CDP(base)
    try:
        if target_id and target_id in page_targets(c):
            return target_id, False
        start = site.get('start_url') or ''
        if not Rules().nav_ok(site, start)[0]:
            raise CDPError('the site has no usable sign-in page address')
        tid = c.call('Target.createTarget', {'url': start, 'background': True}).get('targetId')
        if not tid:
            raise CDPError('Chrome did not open a tab')
        return tid, True
    finally:
        c.close()


def close_tab(base: str, target_id: str) -> bool:
    try:
        c = CDP(base)
    except Exception:
        return False
    try:
        if target_id in page_targets(c):
            c.call('Target.closeTarget', {'targetId': target_id})
            return True
        return False
    except Exception:
        return False
    finally:
        c.close()


# ---- opening and closing sessions -------------------------------------------------------------------------------------

def open_session(site: str, life: Path | None = None, cdp: str | None = None) -> dict:
    """A new session for this site (an open one is replaced: its token dies). Opens or reuses the handoff's own tab;
    if Chrome is not reachable now, the tab is opened when the owner presses Start. -> the session (with its token)."""
    life = Path(life or life_home())
    table = site_table(life)
    if site not in table:
        raise SystemExit(f'unknown site: {site} (known: {", ".join(sorted(table))})')
    entry = table[site]
    base = cdp or cdp_base(life)
    old = Sessions(life).get(site)
    target, created, note = None, False, None
    try:
        target, created = ensure_tab(base, entry, (old or {}).get('target_id') if (old or {}).get('target_ours') else None)
    except Exception as e:
        note = f'tab not opened yet ({type(e).__name__})'
    token = secrets.token_urlsafe(32)
    now = _now()
    s = {'site': site, 'name': entry['name'], 'token': token, 'id': public_id(token), 'state': 'open', 'opened_at': iso(now),
         'opened_ts': now, 'expires_at': iso(now + TTL), 'expires_ts': now + TTL, 'target_id': target,
         'target_ours': bool(created or ((old or {}).get('target_ours') and target and target == (old or {}).get('target_id')))}

    def put(d):
        prev = d['sessions'].get(site)
        d['sessions'][site] = s
        return prev
    prev = Sessions(life).change(put)
    if prev and prev.get('state') == 'open':
        log(life, 'replace', site=site, id=prev.get('id'), by=s['id'])
    log(life, 'open', site=site, id=s['id'], tab='opened' if created else 'reused' if target else 'later', note=note)
    return s


def current_or_open(site: str, life: Path | None = None, cdp: str | None = None) -> dict:
    life = Path(life or life_home())
    s = Sessions(life).get(site)
    if s and s.get('state') == 'open' and s.get('token') and float(s.get('expires_ts') or 0) > _now() + 15 * 60:
        return s
    return open_session(site, life, cdp)


def link_line(site: str, life: Path | None = None, cdp: str | None = None) -> str:
    """The line Zeus sends instead of "Sign in to <store> once in my Chrome". Reuses an open session with at least
    15 minutes left, so asking twice never kills a link already sent."""
    s = current_or_open(site, life, cdp)
    return ask_line(s['name'], link_for(s['token']))


def close_session(site: str, why: str = 'closed', life: Path | None = None, cdp: str | None = None, state: str = 'closed') -> bool:
    life = Path(life or life_home())
    before = Sessions(life).close(site, state, why)
    if not before:
        return False
    if before.get('target_ours') and before.get('target_id'):
        try:
            close_tab(cdp or cdp_base(life), before['target_id'])
        except SystemExit:
            pass
    log(life, 'expire' if state == 'expired' else 'close', site=site, id=before.get('id'), why=why)
    return True


def status(life: Path | None = None) -> dict:
    life = Path(life or life_home())
    out = {}
    for site, s in Sessions(life).load()['sessions'].items():
        out[site] = {k: v for k, v in s.items() if k not in ('token',)}
        out[site]['live'] = s.get('state') == 'open' and float(s.get('expires_ts') or 0) > _now()
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__.split('Environment:')[0].strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'status':
        print(json.dumps(status(), indent=1))
        return 0
    if not rest:
        print(f'usage: handoff.py {cmd} <site>', file=sys.stderr)
        return 2
    site = rest[0]
    if cmd == 'open':
        s = open_session(site)
        print(link_for(s['token']))
        print(sms_link(s['name']))
        print(f"works until {clock(s['expires_ts'])} ({s['expires_at']})")
        return 0
    if cmd == 'link':
        s = current_or_open(site)
        line = ask_line(s['name'], link_for(s['token']))
        if '--json' in rest:
            print(json.dumps({'line': line, 'link': link_for(s['token']), 'sms': sms_link(s['name']), 'sms_text': sms_text(s['name']),
                              'expires_at': s['expires_at'], 'site': site, 'name': s['name']}, indent=1))
        else:
            print(line)
        return 0
    if cmd == 'close':
        print('closed' if close_session(site, 'closed from the command line') else 'no open session')
        return 0
    print(f'unknown command: {cmd}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
