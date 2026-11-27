"""STOP for saved Zeus work: plain words work, the owner's stop reaches all their work, and nobody else can stop it.

Skipped automatically when the pinned Hermes Agent is not importable. Profiles, stores and records live in
tmp_path. Made-up numbers only.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src' / 'zeus_ai'
PLUGIN = PKG / 'plugins' / 'zeus-work'
ENGINE = PKG / 'engine'
OWNER = '+15550100001'
STRANGER = '+15550100002'
OWNER_CHAT = 'any;-;' + OWNER

pytest.importorskip('hermes_cli.plugins', reason='Hermes Agent is not importable here')
pytest.importorskip('gateway.session_context', reason='Hermes Agent is not importable here')
from test_public_hermes import _pinned_hermes  # noqa: E402

if not _pinned_hermes():
    pytest.skip('installed Hermes is not the version Zeus pins', allow_module_level=True)

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
import settings  # noqa: E402

PACKAGE = '_test_zeus_work_stop'
_pkg = types.ModuleType(PACKAGE); _pkg.__path__ = [str(PLUGIN)]; _pkg.__package__ = PACKAGE
sys.modules[PACKAGE] = _pkg
runtime_mod = importlib.import_module(PACKAGE + '.runtime')
store_mod = importlib.import_module(PACKAGE + '.store')


class Ctx:
    profile_name = 'zeus'

    def get_config(self, key, default=None): return default


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / 'profiles' / 'zeus').resolve(); h.mkdir(parents=True)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(h)
    monkeypatch.setattr(settings, 'OWNER_PHONE', OWNER)
    monkeypatch.delenv('ZEUS_OWNER_ALLOW_CLI', raising=False)
    yield h
    reset_hermes_home_override(token)


def _source(home, author, chat_id, platform='photon', kind='private_owner', origin='direct', text=''):
    return {'session_id': 's-' + chat_id, 'turn_id': 't1', 'profile_home': str(home), 'profile': 'zeus',
            'platform': platform, 'text': text, 'origin': origin, 'author_id': author, 'chat_id': chat_id,
            'audience': {'kind': kind, 'profile_home': str(home)}}


def record(store, ident, chat_id=OWNER_CHAT, author=OWNER):
    payload = {'id': ident, 'title': 'Video ' + ident, 'state': 'open', 'generation': 'g0', 'revision': 0,
               'next_action': 'render', 'external_ref': None, 'source': _source(store.home, author, chat_id)}
    with store.db() as db:
        db.execute('INSERT INTO work(id,source_key,payload) VALUES(?,?,?)', (ident, 'k-' + ident, json.dumps(payload)))


def turn(rt, monkeypatch, home, text, author=OWNER, chat_id=OWNER_CHAT, **kw):
    source = _source(home, author, chat_id, text=text, **kw)
    monkeypatch.setattr(rt, 'source', lambda: source)
    monkeypatch.setattr(runtime_mod.Native, 'capture_foreground', lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(runtime_mod.Native, 'repair_pending', lambda self, *a, **k: None, raising=False)
    return rt.before_turn(platform=source['platform'], session_id=source['session_id'], turn_id='t1')


def states(store):
    return {r['id']: r['state'] for r in store.all()}


A, B = 'work_aaaaaaaaaaaa', 'work_bbbbbbbbbbbb'


def test_plain_words_stop_from_the_owner(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx()); store = store_mod.Store(home)
    record(store, A)
    ctx = turn(rt, monkeypatch, home, 'please stop the video')
    assert states(store)[A] == 'stopped'
    assert 'every chat' in ctx['context']


def test_exact_job_id_scopes_the_owner_stop(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx()); store = store_mod.Store(home)
    record(store, A); record(store, B)
    turn(rt, monkeypatch, home, 'stop ' + B)
    assert states(store) == {A: 'open', B: 'stopped'}


def test_an_id_alone_or_ordinary_words_are_not_a_stop(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx()); store = store_mod.Store(home)
    record(store, A)
    turn(rt, monkeypatch, home, A)
    turn(rt, monkeypatch, home, 'can you stop by the cafe in the video')
    assert states(store)[A] == 'open'


@pytest.mark.parametrize('who', [
    dict(author=STRANGER, chat_id='any;-;' + STRANGER),                     # another number
    dict(author=OWNER, chat_id='group-chat', kind='native_remote'),          # not the owner's private audience
    dict(author=OWNER, chat_id=OWNER_CHAT, platform='slack'),                # another platform
    dict(author=OWNER, chat_id=OWNER_CHAT, origin='relayed'),                # not a direct message
    dict(author='', chat_id=OWNER_CHAT),                                     # garbled sender
    dict(author=OWNER, chat_id=OWNER_CHAT, origin='local_operator'),         # CLI without owner.allow_cli
])
def test_nobody_else_can_stop_or_see_the_owners_work(home, monkeypatch, who):
    rt = runtime_mod.Runtime(Ctx()); store = store_mod.Store(home)
    record(store, A)
    turn(rt, monkeypatch, home, 'stop', **who)
    assert states(store)[A] == 'open'
    assert rt.visible_records(store.all(), _source(home, text='stop', **who)) == []


def test_unconfigured_owner_means_nobody_can_stop(home, monkeypatch):
    monkeypatch.setattr(settings, 'OWNER_PHONE', '')
    rt = runtime_mod.Runtime(Ctx()); store = store_mod.Store(home)
    record(store, A)
    turn(rt, monkeypatch, home, 'stop')
    assert states(store)[A] == 'open'


class _V:
    def __init__(self, v): self.value = v


class _Src:
    def __init__(self, user_id, chat_id, chat_type='dm'):
        self.platform = _V('photon'); self.user_id = user_id; self.chat_id = chat_id
        self.chat_type = chat_type; self.is_bot = False; self.thread_id = None; self.parent_chat_id = None


class _Ev:
    internal = False

    def __init__(self, text, source): self.text = text; self.source = source


def test_gateway_stop_works_in_a_busy_chat_only_for_the_owner(home):
    rt = runtime_mod.Runtime(Ctx()); store = store_mod.Store(home)
    record(store, A)
    assert rt.admission(event=_Ev('Stop everything!', _Src(STRANGER, 'any;-;' + STRANGER)), gateway=None) is None
    assert rt.admission(event=_Ev('Stop everything!', _Src(OWNER, 'room', chat_type='group')), gateway=None) is None
    assert states(store)[A] == 'open', 'not the owner: nothing changes'
    assert rt.admission(event=_Ev('Stop everything!', _Src(OWNER, OWNER_CHAT)), gateway=None) is None
    assert states(store)[A] == 'stopped'
