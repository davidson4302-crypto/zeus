"""purchase-lock: Zeus may place an order only with the owner's yes to that store and total, once.
At a booking store (a store with a "booking" block in stores.json) the yes also binds the trip: dates, flights,
airports and passengers (times and fare when given). The check itself is in buyfence.py + booking.py.

How it attaches on public Hermes Agent (one pre_tool_call hook, no Hermes file edited):
  1. browser_exec: the hook rewrites the call (a pre_tool_call "modify") so a short prelude runs before Zeus's code in
     the browser child. The prelude loads buyfence.py and arms its Fence in front of browser_harness.helpers._send,
     the one pipe every browser helper uses; from then on each request that could press something is judged against
     the page it would land on before the browser gets it. If buyfence cannot load, the step does not run at all.
  2. Before that, Zeus's code itself is read: code that could reach the browser around that pipe (the harness's
     private parts, sockets, subprocesses, dynamic imports, exec/eval/getattr ...) is refused. The child does the
     same for agent_helpers.py in the browser workspace.
  3. Only the owner's own chat turn may press (engine/zeus_hermes.py, the one owner test). For any other turn (a
     cron job, a delegated helper, another sender, anything that cannot be told) the prelude says so and the Fence
     refuses every purchase press, whatever grants exist.
  4. Every other way in: browser_cdp input or scripts, browser_dialog accept and computer_use input are refused while
     a store or checkout tab is open (or the tabs cannot be read); shell commands that talk to the browser directly
     (CDP port, harness socket, puppeteer, playwright ...) are always refused.

Only Zeus: the live gateway serves several profiles in one process, so the hook acts only while the active profile home
(get_hermes_home(), which follows the served profile) is Zeus's. Every other agent's tool calls are left as they were.

Switch files in the life folder: buy-lock-off = do nothing; buy-lock-shadow = log, refuse nothing. No switch = enforce.
Settings (env, each overriding engine/settings.py): BUY_PROFILE_HOME (whose steps are fenced, default
settings.PROFILE_DIR), BUY_LIFE_HOME (life folder, default settings.LIFE_HOME), BUY_FENCE_PATH, BUY_STORES_FILE,
BUY_CDP_URL (where tabs are listed).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 'purchase-lock-3.0'
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

SHELL_TOOLS = {'terminal', 'execute_code', 'process_manage', 'bash', 'shell'}
DIRECT = re.compile(r'9333|agents-chrome|Input\.dispatch|browser[-_]harness|agent-browser|remote-debugging|devtools/(?:browser|page)|'
                    r'BU_CDP|BU_NAME|/json/(?:list|version|new)|chrome-remote-interface|puppeteer|playwright|pychrome|'
                    r'bu(?:-[\w-]+)?\.sock', re.I)
INPUT_WORDS = re.compile(r'click|type|key|press|enter|return|drag|set_value', re.I)
READ_ONLY_BROWSER = {'browser_vault'}     # lists logins and fills boxes; never presses


def fence_path() -> Path:
    return Path(os.environ.get('BUY_FENCE_PATH') or HERE / 'buyfence.py')


def stores_file() -> Path:
    return Path(os.environ.get('BUY_STORES_FILE') or HERE / 'stores.json')


def life() -> Path:
    return Path(os.environ.get('BUY_LIFE_HOME') or LIVE_LIFE)


def zeus_active() -> bool:
    """Is the call being run for Zeus's profile? Unknown counts as no: other agents are never touched."""
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


def owner_turn() -> bool:
    """The one owner test (engine/zeus_hermes.py). Anything that goes wrong answers no."""
    try:
        if str(ENGINE_DIR) not in sys.path:
            sys.path.insert(0, str(ENGINE_DIR))
        import zeus_hermes
        return zeus_hermes.is_owner_turn() is True
    except Exception:
        return False


def _log(row: dict):
    try:
        life().mkdir(parents=True, exist_ok=True)
        with open(life() / 'buy-log.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'at': time.time(), 'version': VERSION, 'where': 'gateway', **row}, default=str, ensure_ascii=False) + '\n')
    except OSError:
        pass


_gateway_fence = {'module': None, 'mtime': None, 'stores': None}


def gateway_fence():
    """buyfence loaded in this (gateway) process for its store table and checks. Raises when it cannot load."""
    path = fence_path()
    mtime = path.stat().st_mtime
    if _gateway_fence['module'] is None or _gateway_fence['mtime'] != mtime:
        spec = importlib.util.spec_from_file_location('_purchase_lock_gateway_fence', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _gateway_fence.update(module=module, mtime=mtime, stores=module.Stores(stores_file()))
    return _gateway_fence['module']


# ---- browser_exec: the prelude ------------------------------------------------------------------------------------------
# No http(s) address may appear in it: browser_exec picks its browser route from the addresses in the code.

PRELUDE = '''# purchase lock (Zeus): every browser input in this step is checked before it is sent
def _plk_arm():
    import importlib.util as _u
    try:
        _s = _u.spec_from_file_location('_zeus_buyfence', __FENCE__)
        _m = _u.module_from_spec(_s)
        _s.loader.exec_module(_m)
        _m.arm(globals(), __SETTINGS__)
    except BaseException as _e:
        print('[purchase lock] Not run. The purchase lock could not start in the browser (' + type(_e).__name__ + ': '
              + str(_e)[:200] + '), so this browser step did not run. Tell the owner in one line that the purchase lock '
              'needs a look.', flush=True)
        raise SystemExit(3)
_plk_arm()
del _plk_arm
'''


def prelude(may_press: bool) -> str:
    settings = {'life': str(life()), 'stores': str(stores_file()), 'version': VERSION, 'may_press': bool(may_press)}
    return PRELUDE.replace('__FENCE__', repr(str(fence_path()))).replace('__SETTINGS__', repr(settings))


# ---- the open tabs (for tools that do not go through the browser child) -------------------------------------------------

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


def hot_tabs(timeout: float = 1.5):
    """Pages (scheme/host/path) of open tabs on a store or a checkout address. None when the tabs cannot be read."""
    try:
        with urllib.request.urlopen(cdp_list_url(), timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception:
        return None
    fence = gateway_fence()
    st = _gateway_fence['stores']
    found = []
    for t in tabs if isinstance(tabs, list) else []:
        if not isinstance(t, dict) or t.get('type') != 'page':
            continue
        url = str(t.get('url') or '')
        host = urlsplit(url).hostname or ''
        if st.for_host(host) or st.any_commit_url(url):
            found.append(fence.public_page(url))
    return found


# ---- the hook -----------------------------------------------------------------------------------------------------------

TEXT = {
    'reaches_around': ('Not run. This browser code uses "{what}", which could reach the browser around the purchase lock. '
                       'Use only the browser helpers (new_tab, goto_url, js, fill_input, click_element, click_at_xy, '
                       'press_key, page_info, capture_screenshot ...) and plain Python. Nothing ran.'),
    'direct_browser': ('Not run. This command talks to the browser directly, which skips the purchase lock. Use '
                       'browser_exec for browser steps; Place order also needs the owner\'s yes first (zeus-buy yes).'),
    'raw_browser_tool': ('Not run. A store or checkout page is open in the browser ({page}), and this tool sends input to it '
                         'outside the purchase lock. Use browser_exec with click_element for buttons; Place order also '
                         'needs the owner\'s yes first (zeus-buy yes).'),
    'lock_error': ('Not run. The purchase lock hit a problem checking this step ({why}), so it did not run. Tell the owner '
                   'in one line that the purchase lock needs a look; do not try another way.'),
}


def raw_tool_risky(name: str, args: dict) -> bool:
    """Could this (non browser_exec) browser tool press something on a page?"""
    if name == 'browser_cdp':
        params = args.get('params') if isinstance(args.get('params'), dict) else {}
        return bool(gateway_fence().request_risk(str(args.get('method') or ''), params))
    if name == 'browser_dialog':
        return str(args.get('action') or 'accept').lower() != 'dismiss'
    if name == 'computer_use':
        return bool(INPUT_WORDS.search(str(args.get('action') or json.dumps(args, default=str))))
    return name.startswith('browser_') and name not in READ_ONLY_BROWSER


def check(name: str, args: dict) -> dict | None:
    """-> None (go ahead), {'action': 'modify', ...} or {'reason', 'page'} for a refusal."""
    if name == 'browser_exec':
        code = str(args.get('code') or '')
        around = gateway_fence().reaches_around(code)
        if around:
            return {'reason': 'reaches_around', 'what': around}
        return {'action': 'modify', 'args': {'code': prelude(owner_turn()) + code}}
    if name in SHELL_TOOLS:
        text = json.dumps(args, default=str)[:20000]
        return {'reason': 'direct_browser'} if DIRECT.search(text) else None
    if name in ('browser_cdp', 'browser_dialog', 'computer_use') or name.startswith('browser_'):
        if not raw_tool_risky(name, args):
            return None
        tabs = hot_tabs()
        if tabs is None:
            return {'reason': 'raw_browser_tool', 'page': 'the open tabs could not be read'}
        return {'reason': 'raw_browser_tool', 'page': tabs[0]} if tabs else None
    return None


def belt(tool_name='', args=None, session_id='', **_):
    if not zeus_active():
        return None
    m = mode()
    if m == 'off':
        return None
    name = str(tool_name or '').split('__')[-1]
    args = args if isinstance(args, dict) else {}
    try:
        verdict = check(name, args)
    except Exception as error:                          # fail closed for anything that can reach the browser
        if name == 'browser_exec' or name.startswith('browser_') or name == 'computer_use' or (
                name in SHELL_TOOLS and DIRECT.search(json.dumps(args, default=str)[:20000])):
            verdict = {'reason': 'lock_error', 'why': f'{type(error).__name__}: {str(error)[:120]}'}
        else:
            _log({'action': 'belt', 'tool': name[:80], 'verdict': 'error', 'error': repr(error)[:200]})
            return None
    if verdict is None:
        return None
    if verdict.get('action') == 'modify':
        return verdict
    reason = verdict['reason']
    text = TEXT[reason].format(what=verdict.get('what', ''), page=verdict.get('page', ''), why=verdict.get('why', ''))
    _log({'action': 'belt', 'tool': name, 'verdict': 'would_refuse' if m == 'shadow' else 'refuse', 'reason': reason,
          'page': verdict.get('page'), 'what': verdict.get('what'), 'session': str(session_id)[:80], 'mode': m})
    if m == 'shadow':
        return {'action': 'modify', 'args': {'code': prelude(owner_turn()) + str(args.get('code') or '')}} \
            if name == 'browser_exec' and reason == 'reaches_around' else None
    return {'action': 'block', 'message': text}


def register(ctx):
    ctx.register_hook('pre_tool_call', belt)
