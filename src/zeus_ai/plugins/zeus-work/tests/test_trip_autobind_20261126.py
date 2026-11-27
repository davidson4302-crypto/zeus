"""Trip auto-bind. Runs on this copy; reuses the soft-sleep test helpers."""
import json
import pytest

from test_trip_soft_20261126 import (native_c, store_c, trip_soft, trip, rec, write_trips, on, seed, gate_env, REQ)  # noqa: F401


@pytest.fixture
def h(tmp_path):
    h = tmp_path / 'c' / 'profiles' / 'zeus'
    (h / 'workspace/life-engine').mkdir(parents=True)
    (h / 'workspace/life-engine/research-flow-on').write_text('')      # registered_trip() needs the live engine switch
    (h / 'plugin-data/zeus-work').mkdir(parents=True)
    write_trips(h, [trip()])
    return h


def setup(h, monkeypatch):
    n, jobs = seed(native_c, store_c, h)
    gate_env(monkeypatch, native_c, h, jobs)
    monkeypatch.setattr(n.store, 'fence', lambda *a, **k: None)     # worker fencing is not what this tests
    return n


def test_switch_off_keeps_old_refusal(h, monkeypatch):
    n = setup(h, monkeypatch)
    with pytest.raises(Exception, match='Verify the native trip ID'):
        n.bind_trip('work_x', {}, 't-2c1e6f23', ['follow'])
    assert trip_soft.bind_hint(h, n.store.get('work_x'), __import__('time').time()) is None


def test_code_checked_match_binds_and_then_sleeps(h, monkeypatch):
    on(h, 'trip-auto-bind-on')
    n = setup(h, monkeypatch)
    hint = trip_soft.bind_hint(h, n.store.get('work_x'), __import__('time').time())
    assert 't-2c1e6f23' in hint and 'follow' in hint and 'flight_watch_steps' in hint
    n.bind_trip('work_x', {}, 't-2c1e6f23', ['follow'])
    r = n.store.get('work_x')
    assert r['flight_monitor']['id'] == 't-2c1e6f23' and r['flight_monitor']['steps'] == ['follow']
    assert n.gate('work_x')['state'] == 'flight_monitor'                   # sleeps by the original bound rule
    assert trip_soft.bind_hint(h, r, __import__('time').time()) is None     # bound: no more hint


def test_wrong_or_ambiguous_trip_refused(h, monkeypatch):
    on(h, 'trip-auto-bind-on')
    write_trips(h, [trip(), trip(tid='t-00000001')])          # same person, same saved words: two matches
    n = setup(h, monkeypatch)
    with pytest.raises(Exception, match='Verify the native trip ID'):
        n.bind_trip('work_x', {}, 't-2c1e6f23', ['follow'])
    write_trips(h, [trip(), trip(tid='t-00000002', words='', legs=[{'ident': 'DL100', 'carrier': 'DL', 'number': '100', 'date': trip()['legs'][0]['date'],
                                                                     'origin': 'JFK', 'dest': 'LAX'}])])
    with pytest.raises(Exception, match='Verify the native trip ID'):
        n.bind_trip('work_x', {}, 't-00000002', ['follow'])                 # request does not point at that trip
    n.bind_trip('work_x', {}, 't-2c1e6f23', ['follow'])                     # the one it does point at


def test_other_person_never_binds(h, monkeypatch):
    on(h, 'trip-auto-bind-on')
    n = setup(h, monkeypatch)
    with n.store.db() as db:
        r = n.store.load(db, 'work_x'); r['source'] = {'text': REQ.replace('Jordan', 'Riley')}
        db.execute('UPDATE work SET payload=? WHERE id=?', (json.dumps(r), 'work_x'))
    with pytest.raises(Exception, match='Verify the native trip ID'):
        n.bind_trip('work_x', {}, 't-2c1e6f23', ['follow'])


def test_wake_context_carries_hint(h, monkeypatch):
    on(h, 'trip-auto-bind-on')
    import importlib
    rt = importlib.import_module('_tripsoft_copy.runtime')
    n = setup(h, monkeypatch)
    r = n.store.get('work_x')
    monkeypatch.setattr(rt, 'home', lambda: h)
    monkeypatch.setattr(rt.Native, 'claim_turn', lambda self, ctx: r)
    monkeypatch.setattr(rt.Runtime, 'put_binding', lambda self, *a, **k: None)
    out = rt.Runtime.before_turn(object.__new__(rt.Runtime), platform='cron', task_id='cron:job1:e1', session_id='s')
    assert out['context'].endswith(trip_soft.bind_hint(h, r, __import__('time').time()))
