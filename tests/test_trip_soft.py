"""Trip soft sleep + optional quiet check.

Runs on this plugin copy. Set ORIG_Zeus_WORK to another zeus-work folder (read-only) to also prove
"switch off = byte-identical behaviour" against it at the gate level; without it that test is skipped.
No network: every quiet-check call is a fake.
"""
import datetime as dt
import importlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sys
import types

import pytest

COPY = Path(__file__).resolve().parents[1] / 'src' / 'zeus_ai' / 'plugins' / 'zeus-work'
ORIG = Path(os.environ['ORIG_Zeus_WORK']) if os.environ.get('ORIG_Zeus_WORK') else None
sys.path.insert(0, str(COPY.parents[1] / 'engine'))
import settings  # noqa: E402


def load(root, name):
    pkg = types.ModuleType(name); pkg.__path__ = [str(root)]; pkg.__package__ = name
    sys.modules[name] = pkg
    return importlib.import_module(name + '.native'), importlib.import_module(name + '.store')


native_c, store_c = load(COPY, '_tripsoft_copy')
trip_soft = importlib.import_module('_tripsoft_copy.flight_owner')
native_o, store_o = load(ORIG, '_tripsoft_orig') if ORIG else (None, None)

REQ = ("My friend Jordan is flying in today. His itinerary is in my email from this morning. "
       "Track his flights, text me at each layover that it's still on time, and tell me when to leave for the airport to pick him up.")
WORDS = "Track his flights, text me at each layover that it's still on time, and tell me when to leave for the airport to pick him up."


def today():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(settings.OWNER_TZ)).date().isoformat()


def leg(ident, status='scheduled', gate='B6', day=None):
    return {'ident': ident, 'carrier': ident[:2], 'number': ident[2:], 'date': day or today(), 'origin': 'ORD', 'dest': 'LAX',
            'closed': False, 'seen': {'dep_gate': gate}, 'told': {}, 'delay': {'minutes': 0},
            'facts': {'status': status, 'status_text': 'Scheduled On time', 'dep_gate': gate, 'arr_gate': '70A',
                      'dep_terminal': '1', 'arr_terminal': '7'}}


def trip(tid='t-2c1e6f23', state='watching', words=WORDS, legs=None):
    return {'id': tid, 'state': state, 'who': 'Jordan', 'words': words, 'legs': legs or [leg('UA1452'), leg('UA318')],
            'fingerprints': [], 'pickup': {'airport': 'LAX', 'leave': {}}, 'failures': 0, 'notes': ['n1']}


def rec(text=REQ, **kw):
    r = {'id': 'work_x', 'state': 'open', 'created_at': __import__('time').time(), 'source': {'text': text}, 'revisions': [],
         'external_ref': None}
    r.update(kw)
    return r


@pytest.fixture
def home(tmp_path):
    h = tmp_path / 'profiles' / 'zeus'
    (h / 'workspace/life-engine').mkdir(parents=True)
    (h / 'plugin-data/zeus-work').mkdir(parents=True)
    return h


def on(home, *names):
    for n in names:
        (home / 'plugin-data/zeus-work' / n).write_text('')


def write_trips(home, trips):
    (home / 'workspace/life-engine/flights.json').write_text(json.dumps({'trips': trips, 'hosts': {}}))


# ---------------------------------------------------------------- decide(): pure rules

def test_switch_off_is_none(home):
    write_trips(home, [trip()])
    assert trip_soft.decide(home, rec(), 1e9) is None


def test_match_by_words_and_by_idents(home):
    on(home, 'trip-soft-sleep-on')
    import time
    t = time.time()
    assert trip_soft.match(rec(), [trip()], t)[1] == 'words'
    r = rec(text='Track UA 1452 and UA318 for me please')
    assert trip_soft.match(r, [trip(words='')], t)[1] == 'idents'
    assert trip_soft.match(rec(text='Track UA1452 for me'), [trip(words='')], t) is None      # one leg named, not all
    assert trip_soft.match(rec(text='Track UA11452 and UA3180'), [trip(words='')], t) is None  # no partial numbers
    other = REQ.replace('Jordan', 'Riley')
    assert trip_soft.match(rec(text=other), [trip()], t) is None                                # same words, other person


def test_no_match_when_two_trips_or_not_watching_or_other_day(home):
    import time
    t = time.time()
    assert trip_soft.match(rec(), [trip(), trip(tid='t-00000001')], t) is None
    assert trip_soft.match(rec(), [trip(state='done')], t) is None
    old = (dt.date.fromisoformat(today()) - dt.timedelta(days=5)).isoformat()
    assert trip_soft.match(rec(), [trip(legs=[leg('UA1452', day=old), leg('UA318', day=old)])], t) is None


def test_bound_items_unaffected(home):
    on(home, 'trip-soft-sleep-on')
    write_trips(home, [trip()])
    assert trip_soft.decide(home, rec(flight_monitor={'id': 't-2c1e6f23'}), 1e9) is None
    assert trip_soft.decide(home, rec(external_ref={'system': 'life-flight', 'id': 't-2c1e6f23'}), 1e9) is None
    assert trip_soft.decide(home, rec(state='reconcile_first'), 1e9) is None


def sequence(home, ask=None):
    """first wake -> unchanged skip -> change wake. Returns the stored mark after each admitted wake."""
    import time
    t0 = time.time()
    r = rec()
    d1 = trip_soft.decide(home, r, t0, ask=ask)
    assert d1['skip'] is False and d1['why'] == 'first_match'
    r['trip_soft'] = d1['mark']
    d2 = trip_soft.decide(home, r, t0 + 60, ask=ask)
    assert d2['skip'] is True and d2['why'] == 'trip_unchanged' and 'mark' not in d2
    return r, t0


def test_unchanged_skips_and_30_min_forces_wake(home):
    on(home, 'trip-soft-sleep-on')
    write_trips(home, [trip()])
    r, t0 = sequence(home)
    assert trip_soft.decide(home, r, t0 + 29 * 60)['skip'] is True
    d = trip_soft.decide(home, r, t0 + 30 * 60)
    assert d['skip'] is False and d['why'] == 'forced_30_min'


def test_change_wakes_and_ignores_check_times(home):
    on(home, 'trip-soft-sleep-on')
    write_trips(home, [trip()])
    r, t0 = sequence(home)
    t = trip(); t['notes'] = ['n1', 'n2 new check time']; t['next_check'] = 'later'
    write_trips(home, [t])
    assert trip_soft.decide(home, r, t0 + 120)['skip'] is True          # notes/check times are not facts
    write_trips(home, [trip(legs=[leg('UA1452', gate='F26'), leg('UA318')])])
    d = trip_soft.decide(home, r, t0 + 180)
    assert d['skip'] is False and d['why'] == 'trip_changed'


def test_trip_ended_wakes_once_then_original_path(home):
    on(home, 'trip-soft-sleep-on')
    write_trips(home, [trip()])
    r, t0 = sequence(home)
    write_trips(home, [trip(state='done')])
    d = trip_soft.decide(home, r, t0 + 120)
    assert d['skip'] is False and d['why'] == 'trip_ended'
    r['trip_soft'] = d['mark']
    assert trip_soft.decide(home, r, t0 + 180) is None                  # back to the unchanged gate


def test_fingerprint_only_on_admitted_wakes(home):
    on(home, 'trip-soft-sleep-on')
    write_trips(home, [trip()])
    r, t0 = sequence(home)
    before = json.dumps(r, sort_keys=True)
    for k in range(5):
        d = trip_soft.decide(home, r, t0 + 120 + k * 60)
        assert d['skip'] and 'mark' not in d
    assert json.dumps(r, sort_keys=True) == before                      # decide never mutates the record


def test_errors_wake(home):
    on(home, 'trip-soft-sleep-on')
    (home / 'workspace/life-engine/flights.json').write_text('{not json')
    assert trip_soft.decide(home, rec(), 1e9) is None                   # None = original gate = wake


# ---------------------------------------------------------------- quiet-check layer

def changed(home):
    on(home, 'trip-soft-sleep-on', 'jev-quiet-on')
    write_trips(home, [trip()])
    r, t0 = sequence(home, ask=lambda s: pytest.fail('the quiet check must not be asked on unchanged'))
    write_trips(home, [trip(legs=[leg('UA1452', gate='F26'), leg('UA318')])])
    return r, t0


def test_jev_quiet_skips_only_at_or_below_010(home):
    r, t0 = changed(home)
    seen = []
    d = trip_soft.decide(home, r, t0 + 120, ask=lambda s: seen.append(s) or 0.10)
    assert d['skip'] is True and d['why'] == 'jev_quiet'
    assert 'Jordan' not in seen[0] and 'BEFORE:' in seen[0] and 'AFTER:' in seen[0]
    assert len(seen[0].split('BEFORE: ')[1].split('\nAFTER: ')[0]) <= 600
    d = trip_soft.decide(home, r, t0 + 120, ask=lambda s: 0.11)
    assert d['skip'] is False and d['why'] == 'trip_changed'


def test_jev_error_and_timeout_wake(home):
    r, t0 = changed(home)
    def boom(s): raise TimeoutError('slow')
    assert trip_soft.decide(home, r, t0 + 120, ask=boom)['skip'] is False
    assert trip_soft.decide(home, r, t0 + 120, ask=lambda s: 1.7)['skip'] is False
    assert trip_soft.decide(home, r, t0 + 120, ask=lambda s: 'x')['skip'] is False


@pytest.mark.parametrize('status_text,word', [('Departed On time', 'departed'), ('Arrived On time', 'arrived'),
                                              ('Cancelled', 'cancel'), ('Diverted', 'diverted'), ('Delayed by 40m', 'delay')])
def test_force_words_wake_without_asking(home, status_text, word):
    r, t0 = changed(home)
    l1 = leg('UA1452'); l1['facts']['status_text'] = status_text
    write_trips(home, [trip(legs=[l1, leg('UA318')])])
    d = trip_soft.decide(home, r, t0 + 120, ask=lambda s: pytest.fail('a force word must not ask the quiet check'))
    assert d['skip'] is False and d['jev']['force_word'] == word


def test_degraded_forces_wake(home):
    r, t0 = changed(home)
    t = trip(legs=[leg('UA1452', gate='F26'), leg('UA318')]); t['failures'] = 3
    write_trips(home, [t])
    d = trip_soft.decide(home, r, t0 + 120, ask=lambda s: 0.0)
    assert d['skip'] is False and d['jev']['force_word'] in ('degraded', 'could not be read')


def test_jev_not_asked_on_30_min_or_ended(home):
    r, t0 = changed(home)
    no = lambda s: pytest.fail('the quiet check runs only on a fingerprint change')
    assert trip_soft.decide(home, r, t0 + 1800, ask=no)['why'] == 'forced_30_min'
    write_trips(home, [trip(state='done')])
    assert trip_soft.decide(home, r, t0 + 120, ask=no)['why'] == 'trip_ended'


def test_jev_verdict_reused_for_same_pair(home):
    r, t0 = changed(home)
    d = trip_soft.decide(home, r, t0 + 120, ask=lambda s: 0.02)
    ev = {'why': d['why'], **d['event']}
    d2 = trip_soft.decide(home, r, t0 + 180, ask=lambda s: pytest.fail('reuse, do not re-ask'), last_skip=ev)
    assert d2['skip'] and d2['event']['jev'] == 'reused'


def test_jev_switch_alone_does_nothing(home):
    on(home, 'jev-quiet-on')
    write_trips(home, [trip()])
    assert trip_soft.decide(home, rec(), 1e9, ask=lambda s: pytest.fail('no')) is None


# ---------------------------------------------------------------- gate() level, original vs copy

class FakeJobs:
    def __init__(self, job): self.job = job; self.paused = []
    def get_job(self, jid): return dict(self.job) if jid == self.job['id'] else None
    def pause_job(self, jid, reason=None): self.paused.append((jid, reason)); return {}


def gate_env(monkeypatch, mod, home, jobs):
    @contextmanager
    def scope(_home):
        yield jobs
    monkeypatch.setattr(mod, 'native_scope', scope)
    cron = types.ModuleType('cron')
    ex = types.ModuleType('cron.executions'); dq = types.ModuleType('cron.delivery_queue')
    ex.latest_execution = lambda jid: {'id': 'exec-' + str(len(jobs.paused)) + '-' + str(gate_env.n), 'status': 'claimed'}
    ex.get_execution = lambda eid: {'id': eid, 'status': 'completed'}
    dq.get_status = lambda eid: None
    cron.executions, cron.delivery_queue = ex, dq
    monkeypatch.setitem(sys.modules, 'cron', cron)
    monkeypatch.setitem(sys.modules, 'cron.executions', ex)
    monkeypatch.setitem(sys.modules, 'cron.delivery_queue', dq)


gate_env.n = 0


def seed(mod, store_mod, home):
    n = mod.Native(home)
    script = home / 'scripts' / 'gate.py'; script.parent.mkdir(exist_ok=True); script.write_bytes(b'print(1)\n')
    job = {'id': 'job1', 'schedule': {'kind': 'interval', 'minutes': 1}, 'enabled': True}
    for k in mod.CONTRACT_KEYS:
        job.setdefault(k, None)
    contract = {k: job.get(k) for k in mod.CONTRACT_KEYS}
    r = rec(id='work_x', generation='g0', revision=1, scope_hash='s1', steps=[{'id': 'follow', 'state': 'pending'}], effects={},
            criteria=[], evidence={}, worker=None,
            schedule={'state': 'acknowledged', 'job_id': 'job1', 'contract': contract, 'script': str(script),
                      'script_sha256': store_mod.digest(script.read_bytes()), 'not_before': 0})
    with n.store.db() as db:
        db.execute('INSERT INTO work(id,source_key,payload) VALUES(?,?,?)', ('work_x', 'k', json.dumps(r)))
    return n, FakeJobs(job)


def strip(rec_or_res):
    s = json.loads(json.dumps(rec_or_res))
    for k in ('generation', 'updated_at', 'execution_id', 'created_at'):
        s.pop(k, None)
    if isinstance(s.get('schedule'), dict):
        s['schedule'].pop('script', None)          # per-test temp path
    if isinstance(s.get('worker'), dict):
        for k in ('generation', 'admitted_at', 'execution_id'):
            s['worker'].pop(k, None)
    return s


def run_ticks(monkeypatch, mod, store_mod, home, ticks=4):
    n, jobs = seed(mod, store_mod, home)
    gate_env(monkeypatch, mod, home, jobs)
    out = []
    for i in range(ticks):
        gate_env.n = i
        res = n.gate('work_x')
        if res.get('wakeAgent'):
            # the admitted turn runs and yields (what runtime.py does after a real turn)
            with n.store.db() as db:
                cur = n.store.load(db, 'work_x'); cur['worker']['state'] = 'yielded'; n.store.save(db, cur, 'worker_yielded', {})
        r = n.store.get('work_x')
        with n.store.db() as db:
            kinds = [k for (k,) in db.execute('SELECT kind FROM events ORDER BY seq')]
        out.append((strip(res), strip(r), kinds))
    return out


@pytest.mark.skipif(ORIG is None, reason='set ORIG_Zeus_WORK to compare against another copy')
def test_gate_switch_off_identical_to_original(tmp_path, monkeypatch):
    results = []
    for mod, sm, name in ((native_o, store_o, 'o'), (native_c, store_c, 'c')):
        h = tmp_path / name / 'profiles' / 'zeus'
        (h / 'workspace/life-engine').mkdir(parents=True)
        write_trips(h, [trip()])                       # a matching trip exists, but the switch is off
        results.append(run_ticks(monkeypatch, mod, sm, h))
    assert results[0] == results[1]
    assert all(res['wakeAgent'] is True for res, _, _ in results[1])
    assert 'trip_soft' not in results[1][-1][1]


def test_gate_switch_on_skips_unchanged_and_stores_mark_only_on_admit(tmp_path, monkeypatch):
    h = tmp_path / 'c' / 'profiles' / 'zeus'
    (h / 'workspace/life-engine').mkdir(parents=True)
    write_trips(h, [trip()])
    (h / 'plugin-data/zeus-work').mkdir(parents=True)
    on(h, 'trip-soft-sleep-on')
    out = run_ticks(monkeypatch, native_c, store_c, h, ticks=3)
    (res1, r1, _), (res2, r2, k2), (res3, r3, k3) = out
    assert res1['wakeAgent'] is True and r1['trip_soft']['trip_id'] == 't-2c1e6f23'
    assert res2 == {'wakeAgent': False, 'state': 'trip_soft_sleep', 'reason': 'trip_unchanged'}
    assert r2['trip_soft'] == r1['trip_soft'] and r3['trip_soft'] == r1['trip_soft']
    assert k3.count('trip_soft_skip') == 2 and k3.count('wake_pending') == 1
    assert r3['steps'] == r1['steps'] and r3.get('flight_monitor') is None   # never claims steps or binds


def test_gate_bound_item_still_sleeps_by_original_rule(tmp_path, monkeypatch):
    """A bound item never reaches the soft check: sleeping_reason handles it exactly as before."""
    h = tmp_path / 'c' / 'profiles' / 'zeus'
    (h / 'workspace/life-engine').mkdir(parents=True)
    write_trips(h, [trip()])
    (h / 'plugin-data/zeus-work').mkdir(parents=True)
    on(h, 'trip-soft-sleep-on', 'jev-quiet-on')
    n, jobs = seed(native_c, store_c, h)
    gate_env(monkeypatch, native_c, h, jobs)
    monkeypatch.setattr(native_c.Native, 'sleeping_reason', lambda self, r: 'Zeus flight watcher owns tracking: t-2c1e6f23')
    res = n.gate('work_x')
    assert res['state'] == 'flight_monitor'
    with n.store.db() as db:
        assert not db.execute("SELECT 1 FROM events WHERE kind='trip_soft_skip'").fetchall()
