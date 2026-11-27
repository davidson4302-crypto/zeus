"""Purchase lock on public Hermes: the pre_tool_call hook arms buyfence in front of the browser-harness pipe.

Zeus's browser_exec code runs the way browser-harness runs it (``from browser_harness.helpers import *`` then the
code), with the real helpers (click_at_xy, js, cdp, fill_input ...). Only the daemon pipe is fake: a made-up page
answers the fence's reads and records every request that reached "the browser". No real browser, no network, no
store site. Skipped when the pinned Hermes Agent (or its browser-harness) is not importable.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src' / 'zeus_ai'
ENGINE = PKG / 'engine'
PLUGIN = PKG / 'plugins' / 'purchase-lock'
OWNER = '+15550100001'
OWNER_CHAT = 'any;-;' + OWNER

hermes = pytest.importorskip('hermes_cli.plugins', reason='Hermes Agent is not importable here')
pytest.importorskip('gateway.session_context', reason='Hermes Agent is not importable here')


def _pinned_hermes() -> bool:
    """These tests target the Hermes version Zeus pins (hermes_patches/series.json). Skip on any other checkout."""
    import inspect
    import subprocess
    from gateway import session_context as _sc
    base = json.loads((PKG / 'hermes_patches/series.json').read_text())['base_commit']
    root = Path(hermes.__file__).resolve().parents[1]
    head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if head:
        return head == base
    return 'turn_event' in inspect.signature(_sc.set_session_vars).parameters


if not _pinned_hermes():
    pytest.skip('installed Hermes is not the version Zeus pins', allow_module_level=True)

# browser-harness keeps its folders where these say (never the real home); set before it is imported.
_BASE = Path(tempfile.mkdtemp(prefix='zeus-purchase-fence-'))
for _key, _sub in (('BH_HOME', 'bh'), ('BH_TMP_DIR', 'bh-tmp'), ('BH_RUNTIME_DIR', 'bh-run'), ('BH_AGENT_WORKSPACE', 'bh-ws')):
    os.environ[_key] = str(_BASE / _sub)
H = pytest.importorskip('browser_harness.helpers', reason='browser-harness is not importable here')

from gateway.platforms.event import MessageEvent  # noqa: E402
from gateway.session_context import clear_session_vars, set_session_vars  # noqa: E402
from agent.subagent_lifecycle import bind_subagent_parent  # noqa: E402
from agent.delegation_context import delegated_child_context  # noqa: E402

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
import settings  # noqa: E402

HOME = _BASE / 'zeus'
HOME.mkdir()
LIFE = _BASE / 'life'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F = _load('_test_buyfence', PLUGIN / 'buyfence.py')

CHECKOUT = 'https://www.walmart.com/checkout/review-order'
PLACE = {'sel': '#place-order', 'label': 'Place order', 'rect': (600, 480, 160, 40)}
CHANGE = {'sel': '#change-address', 'label': 'Change', 'tag': 'a', 'rect': (100, 200, 80, 20)}
CHECKOUT_TEXT = 'Review your order\nPaper towels, 12 rolls\nEstimated total $42.17'


# ---- a made-up browser behind the harness pipe ---------------------------------------------------------------------

class FakeBrowser:
    """Answers the harness pipe like the daemon would. `sent` = every request that reached the browser."""

    def __init__(self, url, text='', elements=(), focus=None):
        self.url, self.text, self.elements, self.focus = url, text, list(elements), focus
        self.sent = []

    def element_at(self, xy):
        for e in self.elements:
            x, y, w, h = e['rect']
            if x <= xy[0] <= x + w and y <= xy[1] <= y + h:
                return e
        return None

    @staticmethod
    def desc(e):
        return {'tag': e.get('tag', 'button'), 'type': e.get('type', ''), 'labels': [e['label']], 'attrs': e.get('attrs', ''),
                'editable': False}

    def probe(self, xy):
        hit = None
        if xy:
            e = self.element_at(xy)
            if e:
                hit = {'self': self.desc(e), 'clickable': self.desc(e), 'frame': e.get('tag') == 'iframe'}
        return {'href': self.url, 'text': self.text, 'hit': hit, 'focus': self.focus, 'fields': [],
                'buttons': [{'labels': [e['label']], 'attrs': e.get('attrs', '')} for e in self.elements]}

    def __call__(self, req, response_timeout=None):
        if 'meta' in req:
            return {'dialog': None} if req['meta'] == 'pending_dialog' else {'targetId': 'T1', 'url': self.url, 'title': 'tab'}
        method, params = req['method'], req.get('params') or {}
        if method == 'Target.getTargets':
            return {'result': {'targetInfos': [{'type': 'page', 'url': self.url, 'targetId': 'T1'}]}}
        if method == 'Runtime.evaluate' and params.get('expression', '').startswith('/*purchase-lock probe*/'):
            args = json.loads(re.search(r'const A=(\{.*?\});', params['expression']).group(1))
            return {'result': {'result': {'type': 'string', 'value': json.dumps(self.probe(args['xy']))}}}
        self.sent.append((method, params))
        if method == 'Runtime.evaluate' and 'getBoundingClientRect' in params.get('expression', ''):
            e = next(e for e in self.elements if json.dumps(e['sel']) in params['expression'])
            x, y, w, h = e['rect']
            return {'result': {'result': {'type': 'object', 'value': [x + w / 2, y + h / 2]}}}
        if method == 'Runtime.evaluate' and 'e.focus();return true' in params.get('expression', ''):
            return {'result': {'result': {'type': 'boolean', 'value': True}}}         # fill_input found its box
        if method == 'Runtime.evaluate' and params.get('expression', '').startswith('JSON.stringify({url:location.href'):
            return {'result': {'result': {'type': 'string', 'value': json.dumps({'url': self.url, 'title': 'tab'})}}}
        if method == 'Browser.getVersion':
            return {'result': {'userAgent': 'Mozilla/5.0 (Macintosh)'}}
        return {'result': {'result': {'type': 'undefined'}}} if method == 'Runtime.evaluate' else {'result': {}}

    def presses(self):
        return [p for m, p in self.sent if m == 'Input.dispatchMouseEvent' and p.get('type') in ('mousePressed', 'mouseReleased')]


def checkout_page():
    return FakeBrowser(CHECKOUT, CHECKOUT_TEXT, [PLACE, CHANGE])


# ---- the plugin, loaded by Hermes ------------------------------------------------------------------------------------

@pytest.fixture(scope='module')
def plugin():
    names = ('HERMES_HOME', 'BUY_PROFILE_HOME', 'BUY_LIFE_HOME', 'BUY_CDP_URL')
    saved = {k: os.environ.get(k) for k in names}
    saved_phone = settings.OWNER_PHONE
    os.environ.update(HERMES_HOME=str(HOME), BUY_PROFILE_HOME=str(HOME), BUY_LIFE_HOME=str(LIFE),
                      BUY_CDP_URL='http://127.0.0.1:1')
    settings.OWNER_PHONE = OWNER
    (HOME / 'plugins').mkdir(exist_ok=True)
    link = HOME / 'plugins' / 'purchase-lock'
    if not link.exists():
        link.symlink_to(PLUGIN)
    (HOME / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['purchase-lock']}}))
    hermes._reset_plugin_managers_for_tests()
    mgr = hermes.get_plugin_manager()
    mgr.discover_and_load(force=True)
    module = next(m for k, m in sys.modules.items() if getattr(m, '__file__', None) and
                  Path(m.__file__).resolve() == (PLUGIN / '__init__.py').resolve())
    yield SimpleNamespace(manager=mgr, module=module)
    hermes._reset_plugin_managers_for_tests()
    settings.OWNER_PHONE = saved_phone
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def clean(plugin):
    """A fresh life folder and the harness's own pipe back after every test."""
    import shutil
    shutil.rmtree(LIFE, ignore_errors=True)
    LIFE.mkdir()
    original = H._send
    yield
    H._send = original
    (Path(os.environ['BH_AGENT_WORKSPACE']) / 'agent_helpers.py').unlink(missing_ok=True)


def hook(tool, args):
    """What Hermes does before a tool call: (block message, modified args)."""
    return hermes._dispatch_pre_tool_call_hooks(tool, args, session_id='s1', turn_id='t1', tool_call_id='c1')


def run_step(browser, code, plugin=None, may_press=True):
    """Run browser_exec code as browser-harness would, after the hook: the prelude, then Zeus's code."""
    if plugin is None:
        full = _load('_test_plk_init', PLUGIN / '__init__.py').prelude(may_press) + code
    else:
        block, modified = hook('browser_exec', {'code': code})
        assert block is None, block
        full = modified['code']
    H._send = browser
    ns = {'__name__': '__main__'}
    exec('from browser_harness.helpers import *', ns)
    try:
        exec(compile(full, '<browser_exec>', 'exec'), ns)
    finally:
        H._send = browser


def give_yes(store='walmart', total='42.17', expires_in=7200, what='paper towels'):
    now = time.time()
    F.Grants(LIFE).save({'version': 1, 'grants': [{
        'id': 'g1', 'store': store, 'total_cents': F.cents(total), 'what': what, 'state': 'approved', 'at': now,
        'expires_at': now + expires_in, 'history': []}]})


def grant_state():
    return F.Grants(LIFE).load()['grants'][0]['state']


def refused(browser, code, reason, **kw):
    with pytest.raises(RuntimeError) as err:
        run_step(browser, code, **kw)
    assert browser.presses() == []
    rows = [json.loads(line) for line in (LIFE / 'buy-log.jsonl').read_text().splitlines()]
    assert rows[-1]['verdict'] == 'refuse' and rows[-1]['reason'] == reason, rows[-1]
    return str(err.value)


@contextmanager
def turn(user=OWNER, cron='', bind_event=True):
    """Bind a gateway turn the way Hermes does: session context vars, its event, and the running agent."""
    source = SimpleNamespace(platform=SimpleNamespace(value='photon'), chat_id=OWNER_CHAT, chat_type='dm', user_id=user,
                             thread_id=None, parent_chat_id=None, is_bot=False, message_id='m1')
    event = MessageEvent(text='yes place it', source=source, message_id='m1', internal=False)
    event._gateway_accepted = True
    tokens = set_session_vars(platform='photon', chat_id=OWNER_CHAT, chat_type='dm', user_id=user, message_id='m1',
                              session_id='s1', session_key='k1', profile='zeus', cron_session=cron,
                              turn_event=event if bind_event else None)
    agent = SimpleNamespace(session_id='s1', _current_turn_id='t1', platform='photon', _parent_session_id=None,
                            _turn_author={'id': user, 'is_bot': False}, _turn_display_kind=None, _user_id=user)
    try:
        with bind_subagent_parent(agent):
            yield
    finally:
        clear_session_vars(tokens)


# ---- the hook loads through Hermes and arms browser_exec -------------------------------------------------------------

def test_hook_loads_through_hermes_plugin_manager(plugin):
    entry = next(p for k, p in plugin.manager._plugins.items() if k == 'purchase-lock' or k.endswith('purchase-lock'))
    assert not entry.error, entry.error
    block, modified = hook('browser_exec', {'code': 'print(page_info())', 'session': 'shop'})
    assert block is None
    assert modified['session'] == 'shop'
    assert modified['code'].startswith('# purchase lock (Zeus)') and modified['code'].endswith('print(page_info())')
    assert 'http://' not in modified['code'] and 'https://' not in modified['code']   # browser_exec routes by URLs
    assert "'may_press': False" in modified['code']                                     # no turn bound: no press


def test_owner_turn_may_press_cron_and_helper_may_not(plugin):
    with turn():
        assert "'may_press': True" in hook('browser_exec', {'code': 'x = 1'})[1]['code']
    with turn(cron='1'):
        assert "'may_press': False" in hook('browser_exec', {'code': 'x = 1'})[1]['code']
    with turn(), delegated_child_context('child-1'):
        assert "'may_press': False" in hook('browser_exec', {'code': 'x = 1'})[1]['code']
    with turn(user='+15550100002'):
        assert "'may_press': False" in hook('browser_exec', {'code': 'x = 1'})[1]['code']


def test_other_profiles_are_left_alone(plugin, monkeypatch):
    monkeypatch.setenv('BUY_PROFILE_HOME', str(_BASE / 'someone-else'))
    assert hook('browser_exec', {'code': 'x = 1'}) == (None, None)


# ---- the one approved press ------------------------------------------------------------------------------------------

def test_approved_store_and_total_press_goes_through_once(plugin):
    give_yes()
    page = checkout_page()
    with turn():
        run_step(page, "click_element('#place-order')", plugin=plugin)
    assert [p['type'] for p in page.presses()] == ['mousePressed', 'mouseReleased']
    assert grant_state() == 'placed_unconfirmed'
    again = checkout_page()
    with turn():
        text = refused(again, "click_element('#place-order')", 'already_ordered', plugin=plugin)
    assert 'Never press Place order again' in text


def test_a_lower_total_than_the_yes_is_fine(plugin):
    give_yes(total='50.00')
    page = checkout_page()
    run_step(page, "click_element('#place-order')")
    assert len(page.presses()) == 2 and grant_state() == 'placed_unconfirmed'


@pytest.mark.parametrize('case,reason', [
    (dict(total='30.00'), 'total_above_his_yes'),
    (dict(store='amazon'), 'store_mismatch'),
    (None, 'purchase_needs_his_yes'),
    (dict(expires_in=-60), 'purchase_needs_his_yes'),
])
def test_place_order_without_a_matching_unspent_yes_is_refused(plugin, case, reason):
    if case is not None:
        give_yes(**case)
    text = refused(checkout_page(), "click_element('#place-order')", reason)
    assert "owner's yes" in text or 'owner' in text
    if case and case.get('expires_in', 1) < 0:
        assert grant_state() == 'expired'
    elif case:
        assert grant_state() == 'approved'          # a refusal never spends the yes


def test_unreadable_total_is_refused(plugin):
    give_yes()
    page = FakeBrowser(CHECKOUT, 'Review your order', [PLACE])
    refused(page, "click_element('#place-order')", 'total_unreadable')
    assert grant_state() == 'approved'


def test_background_job_or_helper_never_presses_even_with_a_yes(plugin):
    give_yes()
    with turn(cron='1'):
        text = refused(checkout_page(), "click_element('#place-order')", 'not_owner_turn', plugin=plugin)
    assert 'background job' in text
    with turn(), delegated_child_context('child-1'):
        refused(checkout_page(), "click_element('#place-order')", 'not_owner_turn', plugin=plugin)
    assert grant_state() == 'approved'


def test_unknown_target_on_a_checkout_page_is_refused(plugin):
    give_yes()
    text = refused(checkout_page(), 'click_at_xy(5, 5)', 'target_unknown')
    assert 'click_element' in text and grant_state() == 'approved'


def test_harmless_click_on_a_checkout_page_goes_through(plugin):
    page = checkout_page()
    run_step(page, "click_element('#change-address')")
    assert len(page.presses()) == 2


# ---- scripts, keys and raw CDP on a checkout page --------------------------------------------------------------------

@pytest.mark.parametrize('code,reason', [
    ('''js("document.querySelector('#place-order').click()")''', 'place_order_by_click_only'),
    ('''js("setTimeout(()=>0, 10)")''', 'checkout_script_not_read_only'),
    ("cdp('Input.dispatchKeyEvent', type='keyDown', key='Enter')", 'place_order_by_click_only'),
    ("press_key('Enter')", 'place_order_by_click_only'),
    ("cdp('Page.navigate', url='javascript:document.forms[0].submit()')", 'place_order_by_click_only'),
    ("cdp('DOM.setOuterHTML', nodeId=1, outerHTML='<img>')", 'checkout_script_not_read_only'),
])
def test_scripts_and_raw_cdp_on_a_checkout_page_are_refused_even_with_a_yes(plugin, code, reason):
    give_yes()
    page = checkout_page()
    with pytest.raises(RuntimeError):
        run_step(page, code)
    assert page.sent == []                          # the refused request never reached the browser
    assert json.loads((LIFE / 'buy-log.jsonl').read_text().splitlines()[-1])['reason'] == reason
    assert grant_state() == 'approved'


def test_reading_and_typing_on_a_checkout_page_go_through(plugin):
    page = checkout_page()
    page.focus = {'tag': 'input', 'type': 'text', 'labels': [], 'attrs': '', 'editable': False}
    run_step(page, "print(js('document.body.innerText.length')); fill_input('#name', 'Pat Test'); "
                   "print(page_info())")
    typed = [p for m, p in page.sent if m == 'Input.dispatchKeyEvent' and p.get('type') == 'char']
    assert ''.join(p['text'] for p in typed) == 'Pat Test'


def test_gateway_raw_browser_tools_refused_while_a_checkout_tab_is_open(plugin, monkeypatch):
    monkeypatch.setattr(plugin.module, 'hot_tabs', lambda timeout=1.5: ['https://www.walmart.com/checkout'])
    block, _ = hook('browser_cdp', {'method': 'Input.dispatchMouseEvent', 'params': {'type': 'mousePressed', 'x': 1, 'y': 1}})
    assert block and 'checkout page is open' in block
    block, _ = hook('browser_cdp', {'method': 'Runtime.evaluate', 'params': {'expression': "document.querySelector('b').click()"}})
    assert block
    assert hook('browser_dialog', {'action': 'accept'})[0]
    assert hook('browser_dialog', {'action': 'dismiss'})[0] is None
    assert hook('browser_cdp', {'method': 'Runtime.evaluate', 'params': {'expression': 'document.title'}})[0] is None
    monkeypatch.setattr(plugin.module, 'hot_tabs', lambda timeout=1.5: None)            # tabs cannot be read
    assert hook('browser_cdp', {'method': 'Input.dispatchKeyEvent', 'params': {'key': 'Enter'}})[0]
    monkeypatch.setattr(plugin.module, 'hot_tabs', lambda timeout=1.5: [])
    assert hook('browser_cdp', {'method': 'Input.dispatchKeyEvent', 'params': {'key': 'Enter'}})[0] is None


def test_code_that_reaches_around_the_fence_is_refused(plugin):
    for code in ('import browser_harness.helpers as h', 'from browser_harness import _ipc',
                 "getattr(cdp, '__glo' + 'bals__')", 'cdp.__globals__', 'ipc.connect("default")', 'import socket',
                 "exec('x=1')", 'import subprocess'):
        block, _ = hook('browser_exec', {'code': code})
        assert block and 'around the purchase lock' in block, code
    assert hook('terminal', {'command': 'curl -s 127.0.0.1:9333/json/list'})[0]
    assert hook('terminal', {'command': 'ls -la'})[0] is None
    assert hook('browser_exec', {'code': 'js("eval(1)")\nprint("__init__")'})[0] is None   # strings are not code


def test_agent_helpers_that_reach_around_stop_the_step(plugin, capsys):
    (Path(os.environ['BH_AGENT_WORKSPACE']) / 'agent_helpers.py').write_text('from browser_harness import helpers as h\n')
    page = checkout_page()
    with pytest.raises(SystemExit):
        run_step(page, "click_element('#change-address')")
    assert page.sent == [] and 'agent_helpers.py' in capsys.readouterr().out


def test_fence_that_cannot_load_stops_the_step(plugin, monkeypatch):
    broken = _BASE / 'broken-fence.py'
    broken.write_text('raise ImportError("made up")\n')
    monkeypatch.setenv('BUY_FENCE_PATH', str(broken))
    page = FakeBrowser('https://example.test/', 'hello', [CHANGE])
    with pytest.raises(SystemExit):
        run_step(page, "click_element('#change-address')")
    assert page.sent == []


# ---- ordinary browsing ----------------------------------------------------------------------------------------------

def test_non_checkout_browsing_goes_through(plugin):
    page = FakeBrowser('https://example.test/articles', 'Some articles', [{'sel': '#more', 'label': 'Read more', 'rect': (10, 10, 50, 20)}])
    run_step(page, "click_element('#more'); press_key('Enter'); js(\"document.querySelector('#more').click()\")")
    assert len(page.presses()) == 2
    assert any(m == 'Input.dispatchKeyEvent' for m, _ in page.sent)
    assert any('click()' in str(p.get('expression')) for m, p in page.sent)


def test_store_page_add_to_cart_goes_through_but_buy_now_needs_a_yes(plugin):
    add = {'sel': '#add', 'label': 'Add to cart', 'rect': (10, 10, 80, 30)}
    buy = {'sel': '#buy', 'label': 'Buy now', 'rect': (10, 60, 80, 30)}
    page = FakeBrowser('https://www.amazon.com/dp/B000TEST', 'Paper towels $19.99', [add, buy])
    run_step(page, "click_element('#add')")
    assert len(page.presses()) == 2
    refused(FakeBrowser(page.url, page.text, [add, buy]), "click_element('#buy')", 'purchase_needs_his_yes')


def test_shadow_switch_logs_but_refuses_nothing(plugin):
    (LIFE / 'buy-lock-shadow').write_text('')
    page = checkout_page()
    run_step(page, "click_element('#place-order')")
    assert len(page.presses()) == 2
    assert json.loads((LIFE / 'buy-log.jsonl').read_text().splitlines()[-1])['verdict'] == 'would_refuse'


def test_booking_yes_without_the_trip_is_refused(plugin):
    give_yes(store='southwest', total='189.00', what='a flight')
    btn = {'sel': '#purchase', 'label': 'Purchase', 'rect': (600, 480, 160, 40)}
    page = FakeBrowser('https://www.southwest.com/air/booking/purchase', 'Trip total $189.00', [btn])
    refused(page, "click_element('#purchase')", 'booking_needs_details')
    assert grant_state() == 'approved'
