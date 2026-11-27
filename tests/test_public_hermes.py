"""Zeus plugins on public Hermes: loaded by Hermes's own plugin loader, owner test fails closed.

Skipped automatically when Hermes Agent is not importable (plain CI). Run it inside a Hermes venv, e.g.
``<hermes venv>/bin/python -m pytest tests/test_public_hermes.py``. Made-up numbers and text only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src' / 'zeus_ai'
ENGINE = PKG / 'engine'
OWNER = '+15550100001'
STRANGER = '+15550100002'
OWNER_CHAT = 'any;-;' + OWNER

# A private Hermes home named like Zeus's profile (bound only while this module's tests run).
_BASE = Path(tempfile.mkdtemp(prefix='zeus-public-hermes-'))
HOME = _BASE / 'zeus'
HOME.mkdir()

hermes = pytest.importorskip('hermes_cli.plugins', reason='Hermes Agent is not importable here')
pytest.importorskip('gateway.session_context', reason='Hermes Agent is not importable here')


def _pinned_hermes() -> bool:
    """These tests target the Hermes version Zeus pins (hermes_patches/series.json). Skip on any other checkout."""
    import inspect
    import subprocess
    from gateway import session_context as _sc
    base = json.loads((Path(__file__).resolve().parents[1] / 'src/zeus_ai/hermes_patches/series.json').read_text())['base_commit']
    root = Path(hermes.__file__).resolve().parents[1]
    head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if head:
        return head == base
    return 'turn_event' in inspect.signature(_sc.set_session_vars).parameters


if not _pinned_hermes():
    pytest.skip('installed Hermes is not the version Zeus pins', allow_module_level=True)

from gateway.platforms.event import MessageEvent  # noqa: E402
from gateway.session_context import clear_session_vars, set_session_vars  # noqa: E402
from agent.subagent_lifecycle import bind_subagent_parent  # noqa: E402
from agent.delegation_context import delegated_child_context  # noqa: E402

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
import settings  # noqa: E402
import zeus_hermes  # noqa: E402

PLUGINS = sorted(p.name for p in (PKG / 'plugins').iterdir() if (p / '__init__.py').exists())
FORK_ONLY_USERS = ['aaa-zeus-scope', 'agentmail', 'imessage-approvals', 'life-items', 'outbound-message-context',
                   'own-agentmail', 'quiet-personal-replies', 'zeus-work']


@pytest.fixture(scope='module')
def manager():
    saved = {k: os.environ.get(k) for k in ('HERMES_HOME',)}
    saved_settings = (settings.OWNER_PHONE, settings.PROFILE)
    os.environ['HERMES_HOME'] = str(HOME)
    settings.OWNER_PHONE = OWNER
    settings.PROFILE = 'zeus'
    (HOME / 'plugins').mkdir(exist_ok=True)
    for name in PLUGINS:
        link = HOME / 'plugins' / name
        if not link.exists():
            link.symlink_to(PKG / 'plugins' / name)
    config = {'plugins': {'enabled': PLUGINS, 'entries': {'imessage-approvals': {'settings': {
        'enabled': True, 'handle': OWNER, 'chats': [OWNER_CHAT], 'transport_profile': 'zeus',
        'route': 'own', 'target': OWNER, 'db_path': str(_BASE / 'approvals.sqlite3')}}}}}
    (HOME / 'config.yaml').write_text(json.dumps(config))
    hermes._reset_plugin_managers_for_tests()
    mgr = hermes.get_plugin_manager()
    mgr.discover_and_load(force=True)
    yield mgr
    hermes._reset_plugin_managers_for_tests()
    settings.OWNER_PHONE, settings.PROFILE = saved_settings
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _source(user=OWNER, chat_type='dm', platform='photon', chat=OWNER_CHAT):
    return SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=chat, chat_type=chat_type,
                           user_id=user, thread_id=None, parent_chat_id=None, is_bot=False, message_id='m1')


def _event(source, internal=False, accepted=True, text='hello there'):
    event = MessageEvent(text=text, source=source, message_id='m1', internal=internal)
    event._gateway_accepted = accepted
    return event


class Agent(SimpleNamespace):
    pass


@contextmanager
def turn(user=OWNER, chat_type='dm', platform='photon', cron='', internal=False, bind_event=True, event=None):
    """Bind a gateway turn the way Hermes does: session context vars, its event, and the running agent."""
    source = _source(user, chat_type, platform)
    event = event if event is not None else _event(source, internal=internal)
    tokens = set_session_vars(platform=platform, chat_id=source.chat_id, chat_type=chat_type, user_id=user,
                              message_id='m1', session_id='s1', session_key='k1', profile='zeus',
                              cron_session=cron, turn_event=event if bind_event else None)
    agent = Agent(session_id='s1', _current_turn_id='t1', platform=platform, _parent_session_id=None,
                  _turn_author={'id': user, 'is_bot': False}, _turn_display_kind=None, _user_id=user)
    try:
        with bind_subagent_parent(agent):
            yield event
    finally:
        clear_session_vars(tokens)


HOOK_CONTEXT = dict(session_id='s1', turn_id='t1', user_message='hello there', platform='photon',
                    parent_session_id='', conversation_history=[{'role': 'user', 'content': 'hello there'}])


def _bind_scope(manager):
    from hermes_cli.plugins import invoke_hook
    invoke_hook('pre_llm_call', **HOOK_CONTEXT)
    import zeus_scope
    return zeus_scope.current_source(str(HOME))


# ---- loading ----------------------------------------------------------------------------------------

def test_every_plugin_loads_through_hermes(manager):
    loaded = {name: p for name, p in manager._plugins.items()}
    for name in PLUGINS:
        entry = next((p for k, p in loaded.items() if k == name or k.endswith('/' + name) or k.endswith(':' + name)), None)
        assert entry is not None, name + ' was not discovered'
        assert not entry.error, name + ': ' + str(entry.error)


def test_no_unknown_hooks(manager):
    from hermes_cli.plugins import VALID_HOOKS
    for name in PLUGINS:
        text = (PKG / 'plugins' / name / 'plugin.yaml').read_text()
        if 'provides_hooks:' not in text:
            continue
        block = text.split('provides_hooks:', 1)[1].split('\n')
        hooks = []
        for line in block[1:]:
            if not line.startswith('  - '):
                break
            hooks.append(line[4:].strip())
        assert set(hooks) <= VALID_HOOKS, (name, set(hooks) - VALID_HOOKS)


# ---- the one owner test -----------------------------------------------------------------------------

def test_owner_dm_on_photon_is_the_owner(manager):
    with turn():
        owner = zeus_hermes.owner_turn()
    assert owner and owner['author_id'] == OWNER and owner['chat_id'] == OWNER_CHAT


@pytest.mark.parametrize('case', ['stranger', 'group', 'cron', 'internal', 'no_event', 'other_platform', 'helper'])
def test_everything_else_is_not_the_owner(manager, case):
    kwargs = {'stranger': {'user': STRANGER}, 'group': {'chat_type': 'group'}, 'cron': {'cron': '1'},
              'internal': {'internal': True}, 'no_event': {'bind_event': False},
              'other_platform': {'platform': 'slack'}, 'helper': {}}[case]
    with turn(**kwargs):
        if case == 'helper':
            with delegated_child_context('s1'):
                assert zeus_hermes.owner_turn() is None
        else:
            assert zeus_hermes.owner_turn() is None


def test_missing_or_garbled_source_is_not_the_owner(manager):
    assert zeus_hermes.owner_turn() is None                         # no turn bound at all
    garbled = _event(None)
    with turn(event=garbled):
        assert zeus_hermes.owner_turn() is None
    mismatched = _event(_source(user=STRANGER))                       # the event disagrees with the bound turn
    with turn(event=mismatched):
        assert zeus_hermes.owner_turn() is None
    for bad in (None, {}, {'platform': 'photon'}, {'platform': 'photon', 'chat_type': 'dm', 'user_id': OWNER},
                {'platform': 'photon', 'chat_type': 'dm', 'chat_id': OWNER_CHAT, 'user_id': OWNER, 'is_bot': True}):
        assert zeus_hermes.is_owner_source(bad) is False


def test_unconfigured_owner_means_nobody(manager, monkeypatch):
    monkeypatch.setattr(settings, 'OWNER_PHONE', '')
    with turn():
        assert zeus_hermes.owner_turn() is None


def test_local_cli_needs_explicit_setting(manager, monkeypatch):
    with turn(platform='cli', user='', bind_event=False):
        assert zeus_hermes.owner_turn() is None
        monkeypatch.setenv('ZEUS_OWNER_ALLOW_CLI', 'true')
        assert zeus_hermes.owner_turn()['local'] is True


# ---- aaa-zeus-scope: private memory scope -----------------------------------------------------------

def test_scope_binds_only_for_the_owner(manager):
    with turn():
        source = _bind_scope(manager)
        assert source and source['author_id'] == OWNER and source['origin'] == 'direct'
        import zeus_scope
        from zeus_scope import memory_gate
        assert zeus_scope.allowed_private(str(HOME), session_id='s1')
        assert memory_gate.allows_recall(HOME, {'hermesHome': str(HOME.resolve()), 'sessionId': 's1', 'turnId': 't1',
                                                 'platform': 'photon', 'userId': OWNER, 'authorIsBot': False,
                                                 'agentContext': 'primary'})


@pytest.mark.parametrize('kwargs', [{'user': STRANGER}, {'chat_type': 'group'}, {'cron': '1'}, {'internal': True}])
def test_scope_refuses_recall_for_anyone_else(manager, kwargs):
    with turn(**kwargs):
        assert _bind_scope(manager) is None
        from zeus_scope import memory_gate
        assert not memory_gate.allows_recall(HOME, {'hermesHome': str(HOME.resolve()), 'sessionId': 's1', 'turnId': 't1',
                                                     'platform': 'photon', 'userId': kwargs.get('user', OWNER),
                                                     'authorIsBot': False, 'agentContext': 'primary'})


def test_scope_grant_does_not_survive_into_another_sender(manager):
    with turn():
        assert _bind_scope(manager) is not None
    with turn(user=STRANGER):
        import zeus_scope
        assert zeus_scope.current_source(str(HOME)) is None


# ---- outbound-message-context and life-items: owner context injection --------------------------------

def _hook(manager, plugin, name):
    for p in manager._plugins.values():
        if getattr(p.manifest, 'name', None) == plugin or str(getattr(p.manifest, 'key', '')).endswith(plugin):
            for cb in getattr(p, 'hooks', {}).get(name, []) if isinstance(getattr(p, 'hooks', None), dict) else []:
                return cb
    from hermes_cli.plugins import iter_hook_callbacks
    for cb in iter_hook_callbacks(name):
        if plugin.replace('-', '_') in (getattr(cb, '__module__', '') or '').replace('-', '_'):
            return cb
    return None


@pytest.mark.parametrize('plugin', ['outbound-message-context', 'life-items'])
def test_owner_context_is_not_injected_for_anyone_else(manager, plugin):
    cb = _hook(manager, plugin, 'pre_llm_call')
    assert cb is not None, plugin
    for kwargs in ({'user': STRANGER}, {'chat_type': 'group'}, {'cron': '1'}, {'internal': True}):
        with turn(**kwargs):
            result = cb(**HOOK_CONTEXT)
        if plugin == 'outbound-message-context':
            assert result is None, kwargs
        else:
            assert 'PRIVATE REFERENCE' not in json.dumps(result or {}), kwargs


def test_life_items_owner_turn_follows_the_one_test(manager):
    module = next(m for k, m in sys.modules.items() if k.endswith('life_items') or k.endswith('life-items'))
    with turn():
        assert module.owner_turn('photon', zeus_hermes.current_event(), {}) is True
    with turn(user=STRANGER):
        assert module.owner_turn('photon', zeus_hermes.current_event(), {}) is False
    with turn(cron='1'):
        assert module.owner_turn('photon', None, {}) is False


# ---- own-agentmail: sending on the owner's behalf ---------------------------------------------------

def test_email_send_on_behalf_refused_for_anyone_else(manager):
    module = next(m for k, m in sys.modules.items() if k.endswith('own_agentmail') or k.endswith('own-agentmail'))
    mail = SimpleNamespace(lock=__import__('threading').RLock(), turns={('s1', 't1'): 'send an email to a@example.test subject Hi body Hello'})
    payload = {'to': ['a@example.test'], 'subject': 'Hi', 'text': 'Hello'}
    cls = next(v for v in vars(module).values() if isinstance(v, type) and hasattr(v, 'direct_request'))
    with turn():
        assert cls.direct_request(mail, payload) is True
    with turn(user=STRANGER):
        assert cls.direct_request(mail, payload) is False
    with turn(cron='1'):
        assert cls.direct_request(mail, payload) is False


# ---- imessage-approvals: only the owner's own reply counts ------------------------------------------

def test_approval_reply_from_anyone_else_is_refused(manager):
    module = next(m for k, m in sys.modules.items() if k.endswith('imessage_approvals') or k.endswith('imessage-approvals'))
    hubs = list(module.registry().HUBS.values())
    hub = hubs[0] if hubs else None
    assert hub is not None, 'approvals hub did not register'
    assert hub._verified(_event(_source(user=STRANGER)), None) is None
    assert hub._verified(_event(_source(chat_type='group')), None) is None
    assert hub._verified(_event(_source(), internal=True), None) is None
    assert zeus_hermes.is_owner_event(_event(_source())) is True


# ---- purchase yes: zeus-buy's reader counts only the owner's own messages ----------------------------

def test_purchase_yes_reader_ignores_other_senders(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE sessions (id TEXT, source TEXT, user_id TEXT, chat_type TEXT)')
        con.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, '
                    'timestamp REAL, platform_message_id TEXT, _compressed_summary INTEGER DEFAULT 0)')
        con.executemany('INSERT INTO sessions VALUES (?,?,?,?)', [('own', 'photon', OWNER, 'dm'),
                                                                  ('other', 'photon', STRANGER, 'dm'),
                                                                  ('room', 'photon', OWNER, 'group')])
        con.executemany('INSERT INTO messages (session_id, role, content, timestamp, platform_message_id) VALUES (?,?,?,?,?)',
                        [('own', 'user', 'yes from the owner', 100.0, 'p1'),
                         ('other', 'user', 'yes from a stranger', 101.0, 'p2'),
                         ('room', 'user', 'yes in a group', 102.0, 'p3')])
    import chats
    monkeypatch.setattr(settings, 'OWNER_PHONE', OWNER)
    words = [m['text'] for m in chats.messages(0, path=str(db)) if m['who'] == 'owner']
    assert words == ['yes from the owner']


# ---- quiet-personal-replies and zeus-work ------------------------------------------------------------

def test_quiet_replies_human_turn_uses_public_turn_metadata(manager):
    module = next(m for k, m in sys.modules.items() if k.endswith('quiet_personal_replies') or k.endswith('quiet-personal-replies'))
    with turn():
        assert 'context' in module.before_turn(user_message='are you free at 3?', parent_session_id='')
    assert zeus_hermes.is_human_turn(None) is False
    assert zeus_hermes.is_human_turn({'id': OWNER, 'is_bot': True}) is False


def test_zeus_work_sources_and_timed_sends_fail_closed(manager):
    native = next(m for k, m in sys.modules.items() if k.endswith('zeus_work.native') or k.endswith('zeus-work.native'))
    # A timed send goes only to the owner's own direct chat (tests/test_timed_returns.py covers the send itself).
    assert native.native_deadline_send(HOME, {'chat_id': OWNER_CHAT, 'author_id': OWNER}, 'op1', 'hi') == {
        'refused': 'not_owner_direct_chat'}
    assert native.native_deadline_send(HOME, {'platform': 'photon', 'chat_id': 'any;-;' + STRANGER,
                                              'author_id': STRANGER}, 'op1', 'hi') == {'refused': 'not_owner_direct_chat'}
    assert native.deadline_receipt(HOME, {}, 'op1')['state'] == 'none'
    src_mod = next(m for k, m in sys.modules.items() if k.endswith('zeus_work.source_adapter') or k.endswith('zeus-work.source_adapter'))
    sources = src_mod.SourceAdapter(SimpleNamespace(get_config=lambda k, d=None: d), HOME)
    with turn(user=STRANGER):
        _bind_scope(manager)
        assert sources.current() is None
    with turn():
        _bind_scope(manager)
        current = sources.current()
        assert current and current['audience']['kind'] == 'private_owner'


# ---- cross-surface-recall: the owner's private chat is recalled only for the owner -------------------

@pytest.mark.parametrize('is_owner,source,expected', [(True, 'photon', 'owner'), (False, 'photon', 'person'),
                                                      (False, 'cli', 'person'), (False, 'cron', 'mail'),
                                                      (None, 'photon', 'person')])
def test_recall_audience_follows_the_owner_test(manager, is_owner, source, expected):
    module = next(m for k, m in sys.modules.items() if k.endswith('cross_surface_recall') or k.endswith('cross-surface-recall'))
    assert module.audience_of(is_owner, source) == expected
    assert module.visible('person', 'Bot Chat', 'photon', OWNER, OWNER_CHAT, 'dm', STRANGER, 'other', 'dm') is False
