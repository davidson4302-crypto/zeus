"""Timed returns on public Hermes: Zeus's own send record, one send per operation, uncertain never resent.

Skipped automatically when the pinned Hermes Agent is not importable. A fake platform sender stands in for the
chat transport: no network, no gateway, no real message. Made-up numbers only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src' / 'zeus_ai'
PLUGIN = PKG / 'plugins' / 'zeus-work'
ENGINE = PKG / 'engine'
OWNER = '+15550100001'
STRANGER = '+15550100002'
OWNER_CHAT = 'any;-;' + OWNER
ORIGIN = {'platform': 'photon', 'chat_id': OWNER_CHAT, 'author_id': OWNER, 'profile': 'zeus',
          'session_id': 's1', 'turn_id': 't1', 'message_id': 'm1'}
REQUEST = 'compare these two plans and send it in 10 minutes'

hermes = pytest.importorskip('hermes_cli.plugins', reason='Hermes Agent is not importable here')
pytest.importorskip('gateway.session_context', reason='Hermes Agent is not importable here')


# The same pin check as the public-Hermes suite (that module skips itself, and so this one, on any other Hermes).
from test_public_hermes import _pinned_hermes  # noqa: E402

if not _pinned_hermes():
    pytest.skip('installed Hermes is not the version Zeus pins', allow_module_level=True)

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
import settings  # noqa: E402

_spec = importlib.util.spec_from_file_location('zeus_work_timed', PLUGIN / '__init__.py',
                                               submodule_search_locations=[str(PLUGIN)])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules['zeus_work_timed'] = _pkg
_spec.loader.exec_module(_pkg)
import zeus_work_timed.native as native  # noqa: E402
import zeus_work_timed.sendrecord as sendrecord  # noqa: E402
from zeus_work_timed.store import Store  # noqa: E402


class Killed(BaseException):
    """Stands in for the process dying in the middle of a send."""


class FakeSender:
    def __init__(self, *results):
        self.results = list(results) or [{'success': True, 'message_id': 'p-1'}]
        self.calls = []

    async def __call__(self, pconfig, chat_id, message, **_):
        self.calls.append((chat_id, message))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result(chat_id, message) if callable(result) else result


class Open:
    """The owner clock in daytime: contact_hold says send now. Records how it was asked."""
    def __init__(self, answer=None):
        self.answer, self.asked = answer, []

    def contact_hold(self, at=None, *, reply=False, life=None):
        self.asked.append(reply)
        return self.answer


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / 'zeus'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))          # never the live profile
    monkeypatch.setenv('LIFE_HOME', str(tmp_path / 'life'))
    (tmp_path / 'life').mkdir()
    monkeypatch.setattr(settings, 'OWNER_PHONE', OWNER)
    clock = Open()
    monkeypatch.setattr(native, '_owner_clock', lambda: clock)
    sender = FakeSender()
    monkeypatch.setattr(native, '_platform_sender', lambda platform: (sender, object(), 0))
    mirrored = []
    import gateway.mirror
    monkeypatch.setattr(gateway.mirror, 'mirror_to_session', lambda *a, **k: mirrored.append((a, k)) or True)
    return type('Env', (), dict(home=home, sender=sender, clock=clock, mirrored=mirrored,
                                life=tmp_path / 'life'))


def timed(home, text='Here is the comparison you asked for.', origin=ORIGIN, at=None):
    store = Store(home)
    at = time.time() if at is None else at
    timing = native.deadline_request(REQUEST, at)
    timing['origin'] = dict(origin)
    source = {'session_id': 's-' + str(at) + str(origin), 'turn_id': 't1', 'text': REQUEST, 'message_id': 'm1'}
    record = store.create(native.deadline_plan(), source, 'zeus', deadline=timing)
    with store.db() as db:
        r = store.load(db, record['id'])
        store.prepare_deadline_return(r, text, 'complete')
        store.save(db, r, 'test_prepared')
    return record['id']


def due_now(home, ident):
    """Let the watcher's next pass run now (as if the retry time had come)."""
    store = Store(home)
    with store.db() as db:
        r = store.load(db, ident)
        r['deadline']['next_check'] = time.time()
        store.save(db, r, 'test_due')


def rows(home):
    import sqlite3
    path = Store(home).root / 'sends.sqlite3'
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute('SELECT operation_id,status,chat_id,attempts,message_id FROM sends').fetchall()


# ---- the send itself -------------------------------------------------------------------------------

def test_due_return_sends_once_to_the_owner(env):
    ident = timed(env.home)
    assert native.Native(env.home).deadline_pass(ident) == {'id': ident, 'question': False, 'state': 'sent'}
    assert env.sender.calls == [(OWNER_CHAT, 'Here is the comparison you asked for.')]
    r = Store(env.home).get(ident)
    assert r['state'] == 'completed' and r['return_state'] == 'sent'
    assert r['deadline']['result']['provider_id'] == 'p-1' and r['deadline']['next_check'] is None
    assert rows(env.home) == [(r['return_execution_id'], 'sent', OWNER_CHAT, 1, 'p-1')]
    assert env.clock.asked == [True]          # asked as a reply to the owner's own request
    assert env.mirrored and env.mirrored[0][0][:2] == ('photon', OWNER_CHAT)


def test_repeat_pass_and_repeat_call_never_resend(env):
    ident = timed(env.home)
    n = native.Native(env.home)
    n.deadline_pass(ident)
    assert n.deadline_pass(ident) is None                 # nothing left to do
    due_now(env.home, ident)
    assert n.deadline_pass(ident)['state'] == 'sent'      # a forced pass only reads the record
    op = Store(env.home).get(ident)['return_execution_id']
    again = native.native_deadline_send(env.home, ORIGIN, op, 'Here is the comparison you asked for.')
    assert again['recorded'] is True and again['send']['state'] == 'sent'
    assert len(env.sender.calls) == 1


def test_crash_between_intent_and_result_is_uncertain_and_goes_to_the_owner(env):
    env.sender.results = [Killed()]
    ident = timed(env.home)
    n = native.Native(env.home)
    with pytest.raises(Killed):
        n.deadline_pass(ident)
    op = Store(env.home).get(ident)['return_execution_id']
    assert rows(env.home)[0][1] == 'intent'               # the intent was on disk before the send
    env.sender.results = [{'success': True, 'message_id': 'p-2'}]
    due_now(env.home, ident)
    assert n.deadline_pass(ident)['state'] == 'uncertain'
    r = Store(env.home).get(ident)
    assert r['state'] == 'needs_person' and r['return_state'] == 'uncertain'
    assert r['owner_message'] == native.DEADLINE_UNCERTAIN and r['deadline']['next_check'] is None
    assert rows(env.home)[0][1] == 'uncertain'
    # Later passes and direct calls still never send it again.
    due_now(env.home, ident)
    n.deadline_pass(ident)
    assert native.native_deadline_send(env.home, ORIGIN, op, 'x')['send']['state'] == 'uncertain'
    assert len(env.sender.calls) == 1


def test_a_sender_error_that_may_have_landed_is_uncertain(env):
    env.sender.results = [{'error': 'sidecar returned 504', 'retryable': False}]
    ident = timed(env.home)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'uncertain'
    assert Store(env.home).get(ident)['state'] == 'needs_person'
    due_now(env.home, ident)
    native.Native(env.home).deadline_pass(ident)
    assert len(env.sender.calls) == 1


def test_a_send_in_flight_elsewhere_is_waited_for_not_judged(env):
    seen = []
    ident = timed(env.home)
    op = Store(env.home).get(ident)['return_execution_id']

    def check_while_sending(chat_id, message):
        seen.append(native.deadline_receipt(env.home, ORIGIN, op)['state'])
        return {'success': True, 'message_id': 'p-3'}
    env.sender.results = [check_while_sending]
    native.Native(env.home).deadline_pass(ident)
    assert seen == ['in_flight']
    assert Store(env.home).get(ident)['state'] == 'completed'


def test_clear_failure_before_send_can_retry(env):
    env.sender.results = [{'error': 'connect refused', 'retryable': True}, {'success': True, 'message_id': 'p-4'}]
    ident = timed(env.home)
    n = native.Native(env.home)
    assert n.deadline_pass(ident)['state'] == 'held'
    r = Store(env.home).get(ident)
    assert r['deadline']['result']['reason'] == 'send_failed_nothing_sent' and r['state'] == 'output_ready'
    assert rows(env.home)[0][1] == 'failed'
    due_now(env.home, ident)
    assert n.deadline_pass(ident)['state'] == 'sent'
    assert len(env.sender.calls) == 2 and rows(env.home)[0][1:] == ('sent', OWNER_CHAT, 2, 'p-4')


def test_platform_unavailable_before_any_intent_can_retry(env, monkeypatch):
    def unavailable(platform):
        raise ValueError("The owner's chat platform is not set up; nothing was sent.")
    monkeypatch.setattr(native, '_platform_sender', unavailable)
    ident = timed(env.home)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'held'
    assert rows(env.home) == []
    monkeypatch.setattr(native, '_platform_sender', lambda platform: (env.sender, object(), 0))
    due_now(env.home, ident)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'sent'


def test_proven_failures_stop_after_a_few_tries_and_tell_the_owner(env):
    env.sender.results = [{'error': 'connect refused', 'retryable': True}]
    ident = timed(env.home)
    n = native.Native(env.home)
    for _ in range(native.DEADLINE_SEND_TRIES):
        due_now(env.home, ident)
        n.deadline_pass(ident)
    r = Store(env.home).get(ident)
    assert r['state'] == 'needs_person' and r['deadline']['result']['state'] == 'failed'
    assert r['owner_message'] == native.DEADLINE_NOT_SENT and r['deadline']['next_check'] is None
    assert len(env.sender.calls) == native.DEADLINE_SEND_TRIES


def test_long_return_is_sent_in_parts_and_a_later_part_failing_is_uncertain(env, monkeypatch):
    monkeypatch.setattr(native, '_platform_sender', lambda platform: (env.sender, object(), 200))
    env.sender.results = [{'success': True, 'message_id': 'p-5'}, {'error': 'reset', 'retryable': True}]
    ident = timed(env.home, text='A finding. ' * 60)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'uncertain'   # part one already landed
    assert len(env.sender.calls) == 2


# ---- destination -----------------------------------------------------------------------------------

@pytest.mark.parametrize('origin', [
    dict(ORIGIN, author_id=STRANGER, chat_id='any;-;' + STRANGER),     # somebody else
    dict(ORIGIN, chat_id='any;-;' + STRANGER),                         # owner's id, another person's chat
    dict(ORIGIN, chat_id='any;+;chat-room-1'),                         # a group chat
    dict(ORIGIN, platform='slack'),                                    # another platform
    dict(ORIGIN, chat_id=''), {}, None])
def test_wrong_destination_is_refused_before_any_send(env, origin):
    assert native.owner_destination(origin) is None
    assert native.native_deadline_send(env.home, origin, 'op-wrong', 'hi') == {'refused': 'not_owner_direct_chat'}
    assert env.sender.calls == [] and rows(env.home) == []


def test_owner_destination_is_the_owner_direct_chat(env):
    assert native.owner_destination(ORIGIN) == ('photon', OWNER_CHAT)


def test_a_timed_return_with_a_wrong_origin_goes_to_the_owner_unsent(env):
    ident = timed(env.home, origin=dict(ORIGIN, chat_id='any;+;chat-room-1'))
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'refused'
    r = Store(env.home).get(ident)
    assert r['state'] == 'needs_person' and r['owner_message'] == native.DEADLINE_NOT_SENT
    assert env.sender.calls == []


def test_same_operation_for_another_message_is_refused(env):
    env.sender.results = [{'error': 'connect refused', 'retryable': True}]
    native.native_deadline_send(env.home, ORIGIN, 'op-same', 'first text')
    env.sender.results = [{'success': True, 'message_id': 'p-6'}]
    out = native.native_deadline_send(env.home, ORIGIN, 'op-same', 'different text')
    assert out['refused'] == 'operation_recorded_for_other_message' and len(env.sender.calls) == 1


# ---- quiet hours -----------------------------------------------------------------------------------

def test_quiet_hours_hold_the_return(env):
    morning = time.time() + 8 * 3600
    env.clock.answer = morning
    ident = timed(env.home)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'held'
    r = Store(env.home).get(ident)
    assert r['deadline']['result']['reason'] == 'quiet_hours' and r['return_state'] == 'held'
    assert r['deadline']['next_check'] <= morning and env.sender.calls == [] and rows(env.home) == []
    env.clock.answer = None                                   # quiet hours over
    due_now(env.home, ident)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'sent'
    assert len(env.sender.calls) == 1


def test_an_unknown_clock_holds(env):
    env.clock.answer = float('inf')
    ident = timed(env.home)
    assert native.Native(env.home).deadline_pass(ident)['state'] == 'held'
    assert env.sender.calls == []


def test_real_owner_clock_quiet_unless_the_owner_texted_first(env, monkeypatch):
    """The engine's own ownertime.contact_hold, unmocked, at 11pm and 1pm on the owner's clock."""
    monkeypatch.undo()                                       # drop the fixture's fake clock and sender
    monkeypatch.setenv('HERMES_HOME', str(env.home))
    monkeypatch.setenv('LIFE_HOME', str(env.life))
    monkeypatch.setattr(settings, 'OWNER_PHONE', OWNER)
    zone = ZoneInfo(settings.OWNER_TZ)
    today = datetime.now(zone).replace(minute=0, second=0, microsecond=0)
    night = today.replace(hour=23).timestamp()
    noon = today.replace(hour=13).timestamp()
    monkeypatch.setattr(time, 'time', lambda: night)
    wait = native.deadline_quiet_hold(env.home)
    assert wait is not None and wait > night
    (env.life / 'last-owner-turn.json').write_text(json.dumps({'at': night - 60}))
    assert native.deadline_quiet_hold(env.home) is None      # the owner texted first: a reply may go
    (env.life / 'last-owner-turn.json').write_text(json.dumps({'at': night - 4 * 3600}))
    assert native.deadline_quiet_hold(env.home) is not None
    monkeypatch.setattr(time, 'time', lambda: noon)
    assert native.deadline_quiet_hold(env.home) is None


# ---- the record itself -----------------------------------------------------------------------------

def test_record_survives_reopen(env):
    ident = timed(env.home)
    native.Native(env.home).deadline_pass(ident)
    op = Store(env.home).get(ident)['return_execution_id']
    fresh = sendrecord.SendRecord(Store(env.home).root)
    assert fresh.lookup(op)['state'] == 'sent' and fresh.lookup(op)['message_id'] == 'p-1'
    assert native.deadline_receipt(env.home, ORIGIN, op)['provider_id'] == 'p-1'


def test_a_real_process_killed_mid_send_leaves_an_uncertain_record(tmp_path):
    """A child process writes intent, then dies inside the send. The parent finds it uncertain and never sends."""
    root = tmp_path / 'record'
    child = textwrap.dedent(f"""
        import importlib.util, os, sys
        spec = importlib.util.spec_from_file_location('sendrecord', {str(PLUGIN / 'sendrecord.py')!r})
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        mod.SendRecord({str(root)!r}).send_once('op-kill', 'photon', 'any;-;+15550100001', 'hi', lambda: os._exit(9))
    """)
    proc = subprocess.run([sys.executable, '-I', '-c', child], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 9, proc.stderr
    record = sendrecord.SendRecord(root)
    assert record.lookup('op-kill')['state'] == 'uncertain'
    called = []
    out = record.send_once('op-kill', 'photon', 'any;-;+15550100001', 'hi', lambda: called.append(1))
    assert out['state'] == 'uncertain' and out['recorded'] is True and called == []


def test_classify_follows_hermes_delivery_state():
    assert sendrecord.classify({'success': True, 'message_id': 'a'}) == ('sent', ['a'], None)
    assert sendrecord.classify({'error': 'x', 'retryable': True})[0] == 'failed'
    assert sendrecord.classify({'error': 'x'})[0] == 'uncertain'
    assert sendrecord.classify({'error': 'x', 'delivery': 'partial', 'delivered_message_ids': ['a']})[:2] == (
        'uncertain', ['a'])
    assert sendrecord.classify(None)[0] == 'uncertain'
    assert sendrecord.classify({})[0] == 'uncertain'          # no error but no confirmed success either
