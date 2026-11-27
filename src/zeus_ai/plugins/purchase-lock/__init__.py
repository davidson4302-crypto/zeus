"""purchase-lock: Zeus may place an order only with the owner's yes to that store and total, once.
At a booking store (a store with a "booking" block in stores.json) the yes also binds the trip: dates, flights,
airports and passengers (times and fare when given). The check itself is in buyfence.py + booking.py.

How it attaches (no Hermes source file is edited):
  1. On load it wraps tools.browser_use_cli._action_receipt_script. That function writes the small script each
     browser_exec runs in its browser child; the wrapper adds a few lines right before `_hermes_actions.execute(`, so
     the child loads buyfence.py (this folder) and calls install() on ActionReceipts before any of Zeus's code runs.
     From then on every click, key, raw script and raw CDP call is judged by buyfence before input is sent.
  2. If buyfence cannot load or install in the child, a small stand-alone fail-safe (FAILSAFE below, carried inside
     the script itself) blocks purchase buttons and keys on store/checkout pages and lets everything else run.
  3. If the anchor line is missing (a Hermes update changed the script) or action receipts are off, the lock cannot
     attach. It says so loudly (gateway log + buy-log.jsonl), each browser_exec gets a guard in front that refuses the
     step while its tab is a store checkout page, and the pre_tool_call belt below refuses browser_exec while any tab
     (CDP /json/list) is on a checkout page.
  4. Belt, always on: terminal / execute_code commands that talk to the browser directly (port 9333, agents-chrome,
     Input.dispatch, browser-harness, agent-browser ...) and raw browser tools (browser_cdp, browser_dialog,
     computer_use clicks) are refused while a checkout page is open.

Only Zeus: the live gateway serves several profiles in one process (multiplex_profiles: true), and the wrap is
process-wide, so both the wrapper and the belt act only while the active profile home (get_hermes_home(), which follows
the served profile) is Zeus's. Every other agent's browser steps and tool calls are left exactly as they were.

Switch files in the life folder: buy-lock-off = do nothing; buy-lock-shadow = log, refuse nothing. No switch = enforce.
Settings (env, each overriding engine/settings.py): BUY_PROFILE_HOME (whose steps are fenced, default
settings.PROFILE_DIR), BUY_LIFE_HOME (life folder, default settings.LIFE_HOME), BUY_FENCE_PATH, BUY_STORES_FILE,
BUY_CDP_URL (where tabs are listed).
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

VERSION = 'purchase-lock-2.2'
HERE = Path(__file__).resolve().parent
ENGINE_DIR = Path(__file__).resolve().parents[2] / 'engine'      # zeus_ai/engine, where settings.py lives


def _live_paths() -> tuple:
    """(profile home, life folder) from engine/settings.py; the default profile layout if it cannot be loaded."""
    try:
        if str(ENGINE_DIR) not in sys.path:
            sys.path.insert(0, str(ENGINE_DIR))
        import settings as _settings
        return Path(_settings.PROFILE_DIR), Path(_settings.LIFE_HOME)
    except Exception:
        profile = Path.home() / '.hermes' / 'profiles' / 'zeus'
        return profile, Path(os.environ.get('LIFE_HOME') or profile / 'workspace' / 'life-engine')


LIVE_PROFILE, LIVE_LIFE = _live_paths()
# Store hosts the stand-alone fail-safe treats as store pages when stores.json cannot be read (same as the shipped table).
DEFAULT_STORE_HOSTS = ('walmart.com', 'samsclub.com', 'amazon.com', 'ralphs.com', 'kroger.com', 'southwest.com')
ANCHOR = '_hermes_actions.execute('
STATE = {'wrapped': False, 'fence_missing': None, 'anchor_in_source': None, 'loaded_at': None}
logger = logging.getLogger('purchase-lock')

SHELL_TOOLS = {'terminal', 'execute_code', 'process_manage', 'bash', 'shell'}
DIRECT = re.compile(r'9333|agents-chrome|Input\.dispatch|browser[-_]harness|agent-browser|remote-debugging|devtools/(?:browser|page)|'
                    r'BU_CDP|/json/(?:list|version|new)|chrome-remote-interface|puppeteer|playwright|pychrome', re.I)
BROWSER_ACT = re.compile(r'click|type|press|fill|select|submit|key|evaluate|dialog|cdp|mouse|tap', re.I)
BROWSER_READ_ONLY = {'browser_vault_list', 'browser_snapshot', 'browser_get_images', 'browser_vision', 'browser_console'}
CHECKOUT_FALLBACK = re.compile(r'/checkout|/purchase|/payment|/gp/buy/|/buy/|/air/booking/(?:purchase|price)', re.I)


def fence_path() -> Path:
    return Path(os.environ.get('BUY_FENCE_PATH') or HERE / 'buyfence.py')


def stores_file() -> Path:
    return Path(os.environ.get('BUY_STORES_FILE') or HERE / 'stores.json')


def life() -> Path:
    return Path(os.environ.get('BUY_LIFE_HOME') or LIVE_LIFE)


def zeus_active() -> bool:
    """Is the step being run for Zeus's profile? Unknown counts as no: other agents are never touched."""
    try:
        from hermes_constants import get_hermes_home
        here = Path(get_hermes_home()).resolve()
    except Exception:
        here = Path(os.environ.get('HERMES_HOME') or '/nonexistent').resolve()
    return here == Path(os.environ.get('BUY_PROFILE_HOME') or LIVE_PROFILE).resolve()


def mode() -> str:
    try:
        if (life() / 'buy-lock-off').exists():
            return 'off'
        if (life() / 'buy-lock-shadow').exists():
            return 'shadow'
    except OSError:
        pass
    return 'enforce'


def store_hosts() -> list:
    """Every store host in the store table (for the fail-safe and the in-step guard); the default list if unreadable."""
    try:
        data = json.loads(stores_file().read_text(encoding='utf-8'))
        hosts = sorted({str(h).lower() for s in data['stores'].values() for h in s.get('hosts', []) if str(h).strip()})
        return hosts or list(DEFAULT_STORE_HOSTS)
    except Exception:
        return list(DEFAULT_STORE_HOSTS)


def settings() -> dict:
    return {'life': str(life()), 'stores': str(stores_file()), 'version': VERSION, 'hosts': store_hosts()}


def _log(row: dict):
    try:
        life().mkdir(parents=True, exist_ok=True)
        with open(life() / 'buy-log.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'at': time.time(), 'version': VERSION, 'where': 'gateway', **row}, default=str, ensure_ascii=False) + '\n')
    except OSError:
        pass


_gateway_fence = {'module': None, 'mtime': None, 'stores': None}


def gateway_fence():
    """buyfence loaded in this (gateway) process for its store table and helpers; None if it cannot load."""
    try:
        path = fence_path()
        mtime = path.stat().st_mtime
        if _gateway_fence['module'] is None or _gateway_fence['mtime'] != mtime:
            spec = importlib.util.spec_from_file_location('_purchase_lock_gateway_fence', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _gateway_fence.update(module=module, mtime=mtime, stores=module.Stores(stores_file()))
        return _gateway_fence['module']
    except Exception:
        return None


# ---- the stand-alone fail-safe (runs in the browser child only when buyfence could not load) ---------------------------

FAILSAFE = r'''
def _plk_failsafe(AR, settings, error):
    import contextlib, json, os, re, time
    from urllib.parse import urlsplit
    life = settings.get('life') or ''
    STRONG = re.compile(r"place\s*(?:your\s*)?order|buy\s*(?:it\s*)?now|pay\s*now|complete\s*(?:your\s*)?(?:purchase|booking)|submit\s*order|"
                        r"confirm\s*(?:and|&)\s*(?:pay|book)|book\s*(?:it\s*|this\s*)?(?:now|trip|flights?)|place.?order|buy.?now|"
                        r"^\s*(?:purchase|pay|buy|book)\s*$", re.I)
    CHECKOUT = re.compile(r"/checkout|/purchase|/payment|/gp/buy/|/buy/|/air/booking/(?:purchase|price)", re.I)
    HOSTS = settings.get('hosts') or ['walmart.com', 'samsclub.com', 'amazon.com', 'ralphs.com', 'kroger.com', 'southwest.com']
    STORE = re.compile(r"(?:^|\.)(?:" + "|".join(re.escape(str(h)) for h in HOSTS) + r")$", re.I)
    PRESS = re.compile(r"click|submit|dispatchEvent|(?:Mouse|Pointer|Keyboard|Touch|Input)Event|\beval\b|Function\s*\(|setTimeout|"
                       r"setInterval|fromCharCode|\batob\b|fetch\s*\(|XMLHttpRequest|sendBeacon|\[\s*['\"`]", re.I)
    PROBE = ("(()=>{const A=__XY__;const t=s=>String(s==null?'':s).replace(/\\s+/g,' ').trim().slice(0,120);"
             "const L=e=>e&&e.getAttribute?[e.innerText,e.value,e.getAttribute('aria-label'),e.title,e.id,e.getAttribute('name'),"
             "e.getAttribute('data-automation-id')].map(t).filter(Boolean):[];let hit=[],frame=false;"
             "if(A){const h=document.elementFromPoint(A[0],A[1]);if(h){frame=/^(IFRAME|FRAME|EMBED|OBJECT)$/.test(h.tagName);"
             "hit=L(h).concat(L(h.closest('button,a[href],input,[role=button]')))}}"
             "const b=Array.from(document.querySelectorAll('button,input[type=submit],[role=button]')).slice(0,300).map(e=>L(e).join(' '));"
             "return JSON.stringify({href:location.href,hit,frame,buttons:b})})()")
    MSG = ('Not placed. The purchase lock hit a problem (it could not start: %s), so it blocks Place order and Pay buttons '
           'until it is fixed. Everything else in the browser still works. Tell the owner in one line that the purchase lock '
           'needs a look; do not try another way.' % type(error).__name__)

    def log(row):
        try:
            with open(os.path.join(life, 'buy-log.jsonl'), 'a', encoding='utf-8') as fh:
                fh.write(json.dumps({'at': time.time(), 'version': 'purchase-lock-2', 'where': 'failsafe', **row}, default=str) + '\n')
        except Exception:
            pass

    def switch():
        try:
            if os.path.exists(os.path.join(life, 'buy-lock-off')):
                return 'off'
            if os.path.exists(os.path.join(life, 'buy-lock-shadow')):
                return 'shadow'
        except Exception:
            pass
        return 'enforce'

    def look(self, xy=None):
        info = self.raw['page_info']()
        if isinstance(info, dict) and info.get('dialog'):
            return {'href': (self.last_state or {}).get('url', ''), 'dialog': True, 'hit': [], 'frame': False, 'buttons': []}
        return json.loads(self.raw['js'](PROBE.replace('__XY__', json.dumps(xy))))

    def hot(p):
        u = urlsplit(str(p.get('href') or ''))
        return bool((u.hostname in ('www.opentable.com','opentable.com') or (u.hostname in ('www.trip.com','trip.com') and '/hotel' in u.path.lower())) or CHECKOUT.search(u.path or '') or STORE.search(u.hostname or '') or p.get('dialog')
                    or any(STRONG.search(b) for b in p.get('buttons') or []))

    def is_press(self, action, ctx, target):
        if action == 'click':
            xy = ctx.get('xy')
            if target is not None:
                r = target()
                if r.get('refused'):
                    return False                      # the receipts layer refuses this click itself
                xy = [r.get('x'), r.get('y')]
            p = look(self, xy)
            if (urlsplit(p.get('href') or '').hostname in ('www.opentable.com','opentable.com') or (urlsplit(p.get('href') or '').hostname in ('www.trip.com','trip.com') and '/hotel' in urlsplit(p.get('href') or '').path.lower())):
                return True
            if p.get('dialog'):
                return hot(p)
            return any(STRONG.search(x) for x in p.get('hit') or []) or bool(p.get('frame') and hot(p))
        if action == 'raw_script' and ctx.get('expression') is not None and not PRESS.search(str(ctx['expression'])):
            return False
        if action == 'raw_cdp' and ctx.get('method') is not None:
            m, prm = str(ctx['method']), ctx.get('params') or {}
            risky = (m.startswith('Input.') and prm.get('type') not in ('mouseMoved', 'mouseWheel')) or m == 'Page.handleJavaScriptDialog' \
                or (m in ('Runtime.evaluate', 'Runtime.callFunctionOn') and PRESS.search(str(prm.get('expression') or prm.get('functionDeclaration') or '')))
            if not risky:
                return False
        if action in ('press_key', 'dispatch_key') and str(ctx.get('key') or 'Enter') not in ('Enter', 'Return', 'NumpadEnter', ' ', '\r', '\n'):
            return False
        if action == 'type_text' and ctx.get('text') is not None and not re.search(r'[\r\n]', str(ctx['text'])):
            return False
        if action in ('press_key', 'dispatch_key', 'type_text', 'raw_script', 'raw_cdp'):
            return hot(look(self))
        return False

    orig = {n: getattr(AR, n) for n in ('run_action', 'click_element', 'click_at_xy', 'js', 'cdp', 'install')}

    @contextlib.contextmanager
    def ctx_of(self, value):
        prev = getattr(self, '_plk_ctx', None)
        self._plk_ctx = value
        try:
            yield
        finally:
            self._plk_ctx = prev

    def run_action(self, action, effect, *, check=None, target=None, raw=False):
        go = lambda: orig['run_action'](self, action, effect, check=check, target=target, raw=raw)
        m = switch()
        if m == 'off':
            return go()
        ctx = getattr(self, '_plk_ctx', None) or {}
        try:
            press = is_press(self, action, ctx, target)
        except Exception as inner:
            press = action in ('click', 'press_key', 'dispatch_key', 'type_text', 'raw_script', 'raw_cdp')
            ctx = dict(ctx, failsafe_error=type(inner).__name__)
        if not press:
            return go()
        log({'action': action, 'verdict': 'would_refuse' if m == 'shadow' else 'refuse', 'reason': 'lock_error',
             'error': repr(error)[:200], 'mode': m})
        if m == 'shadow':
            return go()
        print('[purchase lock] ' + MSG, flush=True)
        before = self.observe()
        record = self.begin(action, before)
        record['purchase_lock'] = {'reason': 'lock_error'}
        record['message'] = MSG
        return self.complete(record, before, before, 'refused', 'lock_error')

    def click_element(self, selector):
        with ctx_of(self, {'kind': 'click', 'selector': selector}):
            return orig['click_element'](self, selector)

    def click_at_xy(self, x, y, button='left', clicks=1):
        with ctx_of(self, {'kind': 'click', 'xy': [x, y]}):
            return orig['click_at_xy'](self, x, y, button, clicks)

    def js(self, expression, *args, **kwargs):
        with ctx_of(self, {'kind': 'raw_script', 'expression': expression}):
            return orig['js'](self, expression, *args, **kwargs)

    def cdp(self, method, *args, **kwargs):
        with ctx_of(self, {'kind': 'raw_cdp', 'method': method, 'params': kwargs}):
            return orig['cdp'](self, method, *args, **kwargs)

    def install(self, namespace):
        orig['install'](self, namespace)
        for name in ('press_key', 'dispatch_key', 'type_text'):
            if name in namespace:
                inner = namespace[name]
                def wrapped(*a, _inner=inner, _name=name, **k):
                    if _name == 'press_key':
                        c = {'kind': _name, 'key': k.get('key', a[0] if a else None)}
                    elif _name == 'dispatch_key':
                        c = {'kind': _name, 'key': k.get('key', a[1] if len(a) > 1 else 'Enter')}
                    else:
                        c = {'kind': _name, 'text': k.get('text', a[0] if a else None)}
                    with ctx_of(self, c):
                        return _inner(*a, **k)
                namespace[name] = wrapped

    AR.run_action, AR.click_element, AR.click_at_xy, AR.js, AR.cdp, AR.install = run_action, click_element, click_at_xy, js, cdp, install
    AR._purchase_lock = 'failsafe'
    log({'action': 'install', 'verdict': 'failsafe', 'reason': 'fence_did_not_load', 'error': repr(error)[:300]})
'''

# Put in front of a browser step when the lock could not attach (anchor missing, receipts off). Runs in the harness
# child with its helpers (page_info, js); refuses the whole step while its tab is a store or checkout page.
PREFIX_GUARD = r'''# purchase lock: the lock could not attach to this browser step
def _plk_prefix_guard():
    import json, re
    from urllib.parse import urlsplit
    why = __WHY__
    hosts = __HOSTS__
    try:
        info = page_info()
        if isinstance(info, dict) and info.get('dialog'):
            href, buttons = '', ''
            dialog = True
        else:
            dialog = False
            href = str(info.get('url') or '')
            buttons = str(js("Array.from(document.querySelectorAll('button,input[type=submit],[role=button]')).slice(0,300).map(e=>(e.innerText||e.value||e.getAttribute('aria-label')||'').trim()).join(' || ')") or '')
    except Exception:
        return
    u = urlsplit(href)
    store = re.search(r"(?:^|\.)(?:" + "|".join(re.escape(h) for h in hosts) + r")$", u.hostname or '', re.I)
    checkout = re.search(r"/checkout|/purchase|/payment|/gp/buy/|/buy/|/air/booking/(?:purchase|price)", u.path or '', re.I)
    strong = re.search(r"place\s*(?:your\s*)?order|buy\s*(?:it\s*)?now|pay\s*now|complete\s*(?:your\s*)?purchase|submit\s*order", buttons, re.I)
    if ((u.hostname or '').lower() in ('opentable.com','www.opentable.com') or ((u.hostname or '').lower() in ('trip.com','www.trip.com') and '/hotel' in u.path.lower())) or dialog or checkout or (store and strong):
        page = (u.scheme + '://' + (u.netloc or '') + (u.path or '')) if u.scheme else 'a store page'
        print('[purchase lock] Not run. The purchase lock is not active in the browser right now (' + why + '), and a store '
              'checkout page is open (' + page + '). Browser steps on store checkout pages wait until it is fixed. Tell the owner '
              'in one line that the purchase lock needs a look; do not try another way.', flush=True)
        raise SystemExit(3)
_plk_prefix_guard()
del _plk_prefix_guard
'''


def block_text() -> str:
    """The lines put in front of `_hermes_actions.execute(` in the browser child's script."""
    return ('# purchase lock: judge every browser input before it runs\n'
            f'_plk_settings = {settings()!r}\n'
            'try:\n'
            f"    _plk_spec = _hermes_import.spec_from_file_location('_zeus_purchase_lock', {str(fence_path())!r})\n"
            '    _plk_mod = _hermes_import.module_from_spec(_plk_spec)\n'
            '    _plk_spec.loader.exec_module(_plk_mod)\n'
            '    _plk_mod.install(_hermes_actions.ActionReceipts, _plk_settings)\n'
            'except BaseException as _plk_error:\n'
            '    try:\n'
            '        _plk_ns = {}\n'
            f"        exec(compile({FAILSAFE!r}, '<purchase-lock-failsafe>', 'exec'), _plk_ns)\n"
            "        _plk_ns['_plk_failsafe'](_hermes_actions.ActionReceipts, _plk_settings, _plk_error)\n"
            '    except BaseException as _plk_error2:\n'
            "        print('[purchase lock] Not run. The purchase lock could not start in the browser (' + type(_plk_error2).__name__ + '), '\n"
            "              'so this browser step did not run. Tell the owner in one line that the purchase lock needs a look.', flush=True)\n"
            '        raise SystemExit(3)\n')


def prefix_guard(why: str) -> str:
    return PREFIX_GUARD.replace('__WHY__', repr(why)).replace('__HOSTS__', repr(store_hosts()))


def _flag(why: str):
    if STATE['fence_missing'] != why:
        logger.error('purchase-lock: the purchase lock is NOT attached to browser_exec (%s). Checkout pages are now '
                     'refused by the pre_tool_call belt until this is fixed.', why)
        _log({'action': 'attach', 'verdict': 'fence_missing', 'reason': why})
    STATE['fence_missing'] = why


def remove_zeus_layers(handler):
    """Compose only known peer wrappers through their own factory. Leave opaque handlers intact."""
    inner = getattr(handler, '__wrapped__', None)
    if not callable(inner):
        return handler
    if getattr(handler, '_purchase_lock', None):
        return remove_zeus_layers(inner)
    if not getattr(handler, '_zeus_purchase_lock', None):
        return handler
    factory = getattr(handler, '__globals__', {}).get('wrap')
    if not callable(factory):
        return handler
    rebuilt = remove_zeus_layers(inner)
    return handler if rebuilt is inner else factory(rebuilt)

def selected_guard_above(this):
    """Keep a guard across the selector swap. Yield once the restored plugin has actually reattached."""
    selected = (Path(os.environ.get('BUY_PROFILE_HOME') or LIVE_PROFILE) / 'plugins' / 'purchase-lock').resolve()
    if selected == HERE:
        return False
    current = getattr(STATE.get('generator_module'), '_action_receipt_script', None)
    seen = set()
    while callable(current) and current is not this and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, '_purchase_lock', None) and getattr(current, '__globals__', {}).get('HERE') == selected:
            return True
        current = getattr(current, '__wrapped__', None)
    return False

def previous_zeus_blocks(original):
    """Exact child blocks supplied by older Zeus closures underneath other profile wrappers."""
    seen = set()
    while callable(original) and id(original) not in seen:
        seen.add(id(original))
        if getattr(original, '_purchase_lock', None):
            module = getattr(original, '__globals__', {})
            make = module.get('block_text')
            # Foreign profiles have their own markers and a different default profile.
            if callable(make) and module.get('LIVE_PROFILE') == LIVE_PROFILE:
                yield make()
        original = getattr(original, '__wrapped__', None)

def wrap(original):
    """-> the wrapped _action_receipt_script (same signature, same return shape)."""
    def _action_receipt_script(code, session, human_fenced=False):
        script, receipt = original(code, session, human_fenced)
        try:
            if not zeus_active() or mode() == 'off' or selected_guard_above(_action_receipt_script):
                return script, receipt
            if receipt is None:
                _flag('action receipts are off in config.yaml')
                return prefix_guard('action receipts are off') + script, receipt
            pos = script.find('\n' + ANCHOR)
            if pos < 0:
                _flag('the browser step script no longer has the line the lock attaches to')
                return prefix_guard('the browser code changed') + script, receipt
            if not fence_path().exists():
                _flag(f'buyfence.py is missing at {fence_path()}')    # the fail-safe below still guards this step
            elif STATE['fence_missing']:
                logger.warning('purchase-lock: attached again (%s cleared)', STATE['fence_missing'])
                _log({'action': 'attach', 'verdict': 'fence_back', 'reason': STATE['fence_missing']})
                STATE['fence_missing'] = None
            prelude = script[:pos + 1]
            for previous in previous_zeus_blocks(original):
                prelude = prelude.replace(previous, '')
            return prelude + block_text() + script[pos + 1:], receipt
        except Exception as error:                      # never break the browser; fall back to the in-step guard
            _flag(f'the lock wrapper failed ({type(error).__name__})')
            return prefix_guard('the lock wrapper failed') + script, receipt

    _action_receipt_script.__wrapped__ = original
    _action_receipt_script._purchase_lock = VERSION
    _action_receipt_script._zeus_guard_source = HERE
    return _action_receipt_script


def attach(module=None) -> bool:
    """Wrap tools.browser_use_cli._action_receipt_script in this process. True when attached."""
    try:
        if module is None:
            import tools.browser_use_cli as module
        STATE['generator_module'] = module
        current = getattr(module, '_action_receipt_script')
        if getattr(current, '_purchase_lock', None) == VERSION and getattr(current,'_zeus_guard_source',None)==HERE:
            STATE['wrapped'] = True
            return True
        current = remove_zeus_layers(current)
        try:
            STATE['anchor_in_source'] = ANCHOR in inspect.getsource(current)
        except (OSError, TypeError):
            STATE['anchor_in_source'] = None
        if STATE['anchor_in_source'] is False:
            _flag('the browser step script no longer has the line the lock attaches to')
        module._action_receipt_script = wrap(current)
        STATE['wrapped'] = True
        STATE['loaded_at'] = time.time()
        if not fence_path().exists():
            _flag(f'buyfence.py is missing at {fence_path()}')
        return True
    except Exception as error:
        _flag(f'could not wrap browser_exec ({type(error).__name__}: {str(error)[:120]})')
        return False


# ---- the belt (pre_tool_call) -----------------------------------------------------------------------------------------

def cdp_list_url() -> str:
    raw = os.environ.get('BUY_CDP_URL', '').strip()
    if not raw:
        try:
            from tools.browser_use_cli import _read_browser_cfg
            raw = str(_read_browser_cfg().get('cdp_url') or '').strip()
        except Exception:
            raw = ''
    raw = raw or 'http://127.0.0.1:9333'
    m = re.match(r'^(?:wss?|https?)://([^/]+)', raw)
    return f'http://{m.group(1) if m else "127.0.0.1:9333"}/json/list'


def checkout_tabs(timeout: float = 1.5) -> list:
    """URLs (scheme/host/path) of open page tabs that are checkout pages. [] when the browser cannot be reached (then
    nothing can be sent to it either)."""
    try:
        with urllib.request.urlopen(cdp_list_url(), timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception:
        return []
    fence = gateway_fence()
    st = _gateway_fence['stores'] if fence else None
    found = []
    for t in tabs if isinstance(tabs, list) else []:
        if not isinstance(t, dict) or t.get('type') != 'page':
            continue
        url = str(t.get('url') or '')
        try:
            hit = st.any_commit_url(url) if st else bool(CHECKOUT_FALLBACK.search(re.sub(r'^[a-z]+://[^/]+', '', url).split('?')[0]))
        except Exception:
            hit = bool(CHECKOUT_FALLBACK.search(url.split('?')[0]))
        if hit:
            found.append(fence.public_page(url) if fence else url.split('?')[0][:120])
    return found


def _risky_tool(name: str, args: dict) -> str:
    """'' = nothing to check; else why this tool call needs a look at the open tabs."""
    text = json.dumps(args, default=str)[:20000] if args else ''
    if name == 'browser_exec':
        return 'fence_missing' if STATE['fence_missing'] else ''
    if name in SHELL_TOOLS:
        cmd = args.get('command') or args.get('code') or args.get('cmd') or text
        return 'direct_browser' if DIRECT.search(str(cmd)) or DIRECT.search(text) else ''
    if name == 'browser_cdp':
        fence = gateway_fence()
        method = str(args.get('method') or '')
        params = args.get('params') if isinstance(args.get('params'), dict) else args
        risky = fence.risky_cdp(method, params) if fence else (method.startswith('Input.') or method.startswith('Runtime.') or method == 'Page.handleJavaScriptDialog')
        return 'raw_browser_tool' if risky else ''
    if name == 'browser_dialog':
        return 'raw_browser_tool' if str(args.get('action') or args.get('accept') or 'accept').lower() not in ('dismiss', 'false', 'cancel') else ''
    if name == 'computer_use':
        return 'raw_browser_tool' if re.search(r'click|key|type|press|enter|return', str(args.get('action') or text), re.I) else ''
    if name.startswith('browser_') and name not in BROWSER_READ_ONLY and not name.startswith('browser_vault') and BROWSER_ACT.search(name):
        return 'raw_browser_tool'
    return ''


BELT_TEXT = {
    'fence_missing': ('Not run. The purchase lock is not active in the browser right now ({why}), and a checkout page is open '
                      '({page}). Browser steps wait until it is fixed. Tell the owner in one line that the purchase lock needs a '
                      'look; do not try another way.'),
    'direct_browser': ('Not run. A checkout page is open in the browser ({page}), and this command talks to the browser '
                       'directly, which skips the purchase lock. Use browser_exec with click_element for browser steps; Place '
                       'order also needs the owner\'s yes first (zeus-buy yes).'),
    'raw_browser_tool': ('Not run. A checkout page is open in the browser ({page}), and this tool sends raw input to it outside '
                         'the purchase lock. Use browser_exec with click_element for buttons; Place order also needs the owner\'s '
                         'yes first (zeus-buy yes).'),
}


def belt(tool_name='', args=None, session_id='', **_):
    try:
        if not zeus_active():
            return None
        m = mode()
        if m == 'off':
            return None
        name = str(tool_name or '').split('__')[-1]
        args = args if isinstance(args, dict) else {}
        why = _risky_tool(name, args)
        if not why:
            return None
        tabs = checkout_tabs()
        if not tabs:
            return None
        text = BELT_TEXT[why].format(page=tabs[0], why=STATE['fence_missing'] or '')
        _log({'action': 'belt', 'tool': name, 'verdict': 'would_refuse' if m == 'shadow' else 'refuse', 'reason': why,
              'page': tabs[0], 'session': str(session_id)[:80], 'mode': m})
        if m == 'shadow':
            return None
        return {'action': 'block', 'message': text}
    except Exception as error:                          # a belt bug never blocks normal work; it is written down
        _log({'action': 'belt', 'tool': str(tool_name)[:80], 'verdict': 'error', 'error': repr(error)[:200]})
        return None


def register(ctx):
    attach()
    ctx.register_hook('pre_tool_call', belt)
