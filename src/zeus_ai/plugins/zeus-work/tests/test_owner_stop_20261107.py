"""Plain-words STOP, and the owner's STOP from any chat, in zeus-work.

Profiles, stores and records live in tmp_path. ZEUS_WORK picks the plugin copy under test.
"""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import types

import pytest

PLUGIN = Path(os.environ.get('ZEUS_WORK', Path(__file__).resolve().parents[1]))
ENGINE = Path(os.environ.get('ENGINE', str(Path.home() / '.hermes' / 'hermes-agent')))
sys.path.insert(0, str(ENGINE))
PACKAGE = '_test_zeus_work'
pkg = types.ModuleType(PACKAGE); pkg.__path__ = [str(PLUGIN)]; pkg.__package__ = PACKAGE
sys.modules[PACKAGE] = pkg
runtime_mod = importlib.import_module(PACKAGE + '.runtime')
store_mod = importlib.import_module(PACKAGE + '.store')

OWNER = {'owner_ids': ['photon:+15550001111', 'slack:UOWNER']}


class Ctx:
    profile_name = 'zeus-test'

    def __init__(self, settings): self.settings = settings

    def get_config(self, key, default=None): return self.settings.get(key, default)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / 'profiles' / 'zeus-test').resolve(); h.mkdir(parents=True)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(h)
    yield h
    reset_hermes_home_override(token)


def record(store, ident, chat_id, author='+15550001111', platform='photon'):
    payload = {'id': ident, 'title': 'Video ' + ident, 'state': 'open', 'generation': 'g0', 'revision': 0, 'next_action': 'render', 'external_ref': None,
               'source': {'origin': 'direct', 'platform': platform, 'author_id': author, 'chat_id': chat_id,
                          'audience': {'kind': 'native_remote', 'profile_home': str(store.home), 'conversation_id': 'c-' + chat_id,
                                       'platform': platform, 'chat_id': chat_id, 'chat_type': 'dm', 'author_id': author,
                                       'authorization_home': str(store.home)}}}
    with store.db() as db:
        db.execute('INSERT INTO work(id,source_key,payload) VALUES(?,?,?)', (ident, 'k-' + ident, json.dumps(payload)))
    return payload


def turn(runtime, monkeypatch, text, chat_id, author='+15550001111', platform='photon'):
    source = {'session_id': 's-' + chat_id, 'turn_id': 't1', 'profile_home': str(runtime_mod.home()), 'profile': 'zeus-test',
              'platform': platform, 'text': text, 'origin': 'direct', 'author_id': author, 'chat_id': chat_id,
              'audience': {'kind': 'native_remote', 'profile_home': str(runtime_mod.home()), 'conversation_id': 'c-' + chat_id,
                           'platform': platform, 'chat_id': chat_id, 'chat_type': 'dm', 'author_id': author,
                           'authorization_home': str(runtime_mod.home())}}
    monkeypatch.setattr(runtime, 'source', lambda: source)
    monkeypatch.setattr(runtime_mod.Native, 'capture_foreground', lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(runtime_mod.Native, 'repair_pending', lambda self, *a, **k: None, raising=False)
    return runtime.before_turn(platform=platform, session_id='s-' + chat_id, turn_id='t1')


def states(store):
    return {r['id']: r['state'] for r in store.all()}


def test_plain_words_stop_in_the_same_chat(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({'owner': OWNER})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg')
    turn(rt, monkeypatch, 'please stop the video', 'imsg')
    assert states(store)['work_aaaaaaaaaaaa'] == 'stopped', 'old code only took "stop", "cancel", "stop all work"'


def test_owner_stop_from_another_chat_stops_all_their_work(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({'owner': OWNER})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg')
    record(store, 'work_bbbbbbbbbbbb', 'group', author='UTEAMMATE', platform='slack')
    ctx = turn(rt, monkeypatch, 'stop', 'slack-dm', author='UOWNER', platform='slack')
    assert states(store) == {'work_aaaaaaaaaaaa': 'stopped', 'work_bbbbbbbbbbbb': 'stopped'}
    assert 'every chat' in ctx['context']


def test_someone_elses_stop_stays_in_their_own_chat(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({'owner': OWNER})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg')
    turn(rt, monkeypatch, 'stop', 'other', author='+15550109999')
    assert states(store)['work_aaaaaaaaaaaa'] == 'open'


def test_exact_job_id_still_scopes_the_stop(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({'owner': OWNER})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg'); record(store, 'work_bbbbbbbbbbbb', 'imsg')
    turn(rt, monkeypatch, 'stop work_bbbbbbbbbbbb', 'imsg')
    assert states(store) == {'work_aaaaaaaaaaaa': 'open', 'work_bbbbbbbbbbbb': 'stopped'}


def test_an_id_alone_or_ordinary_words_are_not_a_stop(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({'owner': OWNER})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg')
    turn(rt, monkeypatch, 'work_aaaaaaaaaaaa', 'imsg')
    turn(rt, monkeypatch, 'can you stop by the cafe in the video', 'imsg')
    assert states(store)['work_aaaaaaaaaaaa'] == 'open'


class V:
    def __init__(self, v): self.value = v


class Src:
    def __init__(self, user_id, platform='photon', is_bot=False):
        self.platform = V(platform); self.user_id = user_id; self.is_bot = is_bot; self.chat_id = 'x'


class Ev:
    internal = False

    def __init__(self, text, source): self.text = text; self.source = source


def test_admission_stop_works_even_in_a_busy_chat(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({'owner': OWNER})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg')
    import gateway.session_identity as si
    monkeypatch.setattr(si, 'identity_of', lambda s: types.SimpleNamespace(runtime_home=home))
    assert rt.admission(event=Ev('Stop everything!', Src('+15550109999')), gateway=None) is None
    assert states(store)['work_aaaaaaaaaaaa'] == 'open', 'not the owner: nothing changes'
    assert rt.admission(event=Ev('Stop everything!', Src('+15550001111')), gateway=None) is None
    assert states(store)['work_aaaaaaaaaaaa'] == 'stopped'


def test_without_owner_settings_the_old_behaviour_is_kept(home, monkeypatch):
    rt = runtime_mod.Runtime(Ctx({})); store = store_mod.Store(home)
    record(store, 'work_aaaaaaaaaaaa', 'imsg')
    turn(rt, monkeypatch, 'stop', 'slack-dm', author='UOWNER', platform='slack')
    assert states(store)['work_aaaaaaaaaaaa'] == 'open'
