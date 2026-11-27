#!/usr/bin/env python3
"""drill_engine: run Zeus's life engine twice against a RESTORED copy of its work, with every door closed.

Started by `zeus-checkpoint drill` in its own process (never imported by it). It answers one question: if this
checkpoint were put back, would work that was due start again exactly once, and would anything be sent?

The restored copy is a throwaway folder laid out like the profile (<sandbox>/profile). The engine code is only read.

Doors, and how each is closed:
  texting line        there is no notify-live file in the restored life folder (zeus-checkpoint leaves it out), and
                      the line itself is a recorder: anything handed to it is counted and fails the drill
  background jobs     the job runner is a recorder that behaves like the scheduler: it lists the job in the RESTORED
                      copy of cron/jobs.json and runs nothing. The engine's own "did it run / was it dropped" checks
                      then read the restored scheduler records, not the live ones
  models              closed: every model call gets no answer
  mail and calendar   closed: the calendar copy is "could not be read", the mail feed is "could not be read", and no
                      new mail is listed
  page reads          the engine reads pages only from the live folder; here they are closed as well
  programs, network   every program start and every network connect is refused
  live files          every hard-coded path under the real home folder is pointed into the sandbox before use, and an
                      audit hook refuses any open, list or database connect that still reaches into the home folder
                      outside the sandbox, and any write that lands outside the sandbox and the temp folders
  credentials         none are in a checkpoint, and the process gets an empty home folder

The second run is six minutes after the first on the engine's clock, so the engine's own "this job is late, was it
dropped?" path is exercised. That is the path that would start the same work twice.

Usage: python3 -I -B drill_engine.py --sandbox DIR --engine DIR --out FILE [--now ISO] [--live-profile DIR] [--owner-tz ZONE]
"""
from __future__ import annotations

import argparse
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import pwd
import re
import sqlite3
import sys
import tempfile
import time
import types
import uuid
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))
REAL_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
LIVE_PROFILE = REAL_HOME + '/.hermes/profiles/zeus'      # replaced by --live-profile (zeus-checkpoint passes the configured one)
LIVE_LIFE = LIVE_PROFILE + '/workspace/life-engine'
OWNER_TZ = 'UTC'                                          # replaced by --owner-tz
SECOND_RUN_AFTER = 6 * 60
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
WHY = 'closed in a restore drill'

_real_datetime = datetime
_real_time = time.time
_mono = time.monotonic
CLOCK = {'base': None, 'mono0': None}
STATE = {'run': 0, 'busy': False}
JOBS, SENDS, ALARMS = [], [], []
DOORS, DOOR_EXAMPLES, CHECKS = {}, [], {}
CFG = {}


def door(kind: str, example: str = '') -> None:
    DOORS[kind] = DOORS.get(kind, 0) + 1
    if example and len(DOOR_EXAMPLES) < 25:
        DOOR_EXAMPLES.append(f'{kind}: {example[:200]}')


# ---- the clock -----------------------------------------------------------------------------------------------------------

def drill_time() -> float:
    return CLOCK['base'] + (_mono() - CLOCK['mono0'])


def set_clock(ts: float) -> None:
    CLOCK['base'], CLOCK['mono0'] = float(ts), _mono()


class _AnyDatetime(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_datetime)


class Clock(_real_datetime, metaclass=_AnyDatetime):
    """datetime for the engine's modules: now() is the drill's clock, everything else is the real class."""

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(drill_time(), tz)

    @classmethod
    def today(cls):
        return cls.fromtimestamp(drill_time())

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(drill_time(), ZoneInfo('UTC')).replace(tzinfo=None)


# ---- the walls -----------------------------------------------------------------------------------------------------------

def _under(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip('/') + '/')


def _outside(raw) -> str:
    """The real path, when it lies under the real home folder and outside everything this drill may touch; else ''."""
    try:
        p = os.fsdecode(raw)
    except TypeError:
        return ''
    if p.startswith('file:'):
        p = p[5:].split('?', 1)[0]
    p = os.path.realpath(p)
    if not _under(p, REAL_HOME):
        return ''
    return '' if any(_under(p, a) for a in CFG['allow']) else p


def _writes(event: str, args) -> bool:
    """Would this change something on disk? (an open for reading, or a read-only database connect, does not)"""
    if event == 'open':
        mode, flags = (args[1] if len(args) > 1 else None), (args[2] if len(args) > 2 else 0)
        if isinstance(mode, str):
            return any(c in mode for c in 'wax+')
        return bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
    if event == 'sqlite3.connect':
        return 'mode=ro' not in os.fsdecode(args[0])
    return True


_FILE_EVENTS = frozenset(('open', 'os.listdir', 'os.scandir', 'sqlite3.connect', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir',
                          'os.chmod', 'os.truncate', 'os.symlink', 'os.link', 'os.utime', 'shutil.rmtree', 'shutil.copyfile', 'shutil.move'))
_NET_EVENTS = frozenset(('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.sendto', 'socket.bind'))
_RUN_EVENTS = frozenset(('subprocess.Popen', 'os.posix_spawn', 'os.exec', 'os.system', 'os.spawn', 'pty.spawn'))


def _hook(event, args):
    if STATE['busy'] or not (event in _FILE_EVENTS or event in _NET_EVENTS or event in _RUN_EVENTS or event == 'socket.connect'):
        return
    STATE['busy'] = True
    try:
        if event in _FILE_EVENTS:
            for a in args[:2]:
                if isinstance(a, (str, bytes, os.PathLike)):
                    bad = _outside(a)
                    if bad:
                        door('a file outside the restored copy', f'{event} {bad}')
                        raise PermissionError(f'{WHY}: {bad}')
            if event != 'os.listdir' and event != 'os.scandir' and args and isinstance(args[0], (str, bytes, os.PathLike)) and _writes(event, args):
                where = os.path.realpath(os.fsdecode(args[0]))
                if not any(_under(where, w) for w in CFG['writable']):
                    door('a write outside the restored copy', f'{event} {where}')
                    raise PermissionError(f'{WHY}: {where}')
        elif event == 'socket.connect':
            addr = args[1] if len(args) > 1 else None
            if isinstance(addr, (str, bytes)):
                bad = _outside(addr)
                if bad:
                    door('a local service', f'{event} {bad}')
                    raise PermissionError(f'{WHY}: {bad}')
            else:
                door('the network', f'{event} {addr!r}')
                raise PermissionError(f'{WHY}: no network')
        elif event in _NET_EVENTS:
            door('the network', event)
            raise PermissionError(f'{WHY}: no network')
        else:
            door('a program', f'{event} {str(args[0])[:80] if args else ""}')
            raise PermissionError(f'{WHY}: no program is started')
    finally:
        STATE['busy'] = False


def closed_program(cmd=None, *a, **k):
    """Stands where subprocess.run and friends were. Refused before anything starts."""
    first = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
    door('a program', os.path.basename(str(first or '?')))
    raise OSError(f'{WHY}: no program is started')


def closed_network(*a, **k):
    door('the network', 'connect')
    raise OSError(f'{WHY}: no network')


def close_doors() -> None:
    import socket
    import subprocess
    for name in ('run', 'Popen', 'call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput'):
        setattr(subprocess, name, closed_program)
    os.system = os.popen = closed_program
    socket.create_connection = closed_network
    socket.getaddrinfo = closed_network
    socket.socket.connect = closed_network
    socket.socket.connect_ex = closed_network
    sys.addaudithook(_hook)


# ---- the engine's modules, dressed as they load ----------------------------------------------------------------------------

def _repoint(value):
    """A hard-coded path under the real home folder -> the same place in the restored profile, or a path that does
    not exist. Anything else is handed back unchanged (None)."""
    s = os.fspath(value)
    if not _under(s, REAL_HOME) or any(_under(s, a) for a in CFG['allow']):
        return None
    if _under(s, LIVE_PROFILE):
        new = CFG['profile'] + s[len(LIVE_PROFILE):]
    else:
        new = CFG['sandbox'] + '/closed' + s[len(REAL_HOME):]
    return Path(new) if isinstance(value, Path) else new


def _closed_model(system=None, prompt=None, schema=None, timeout=150, *a, **k):
    door('a model')
    return None


def _closed_reader(*a, **k):
    door('a web page')
    return {'ok': False, 'why': WHY}


def _closed_feed(*a, **k):
    raise RuntimeError(f'the mail feed is {WHY}')


class Line:
    """Zeus's texting line, as a recorder. Nothing is live in a drill, so anything that arrives here is a failure."""

    def send(self, text):
        SENDS.append({'run': STATE['run'], 'chars': len(str(text))})
        return {'kind': 'sent'}

    def readback(self, text, since):
        return None


def create_job(prompt, run_at, name):
    """The job runner, as a recorder that behaves like the scheduler: the job is listed (in the restored copy of the
    scheduler's list) and nothing runs."""
    jid = uuid.uuid4().hex[:12]
    JOBS.append({'id': jid, 'run': STATE['run'], 'run_at': str(run_at)})
    path = Path(CFG['profile']) / 'cron/jobs.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {'jobs': []}
    jobs = data.setdefault('jobs', []) if isinstance(data, dict) else data
    jobs.append({'id': jid, 'name': 'restore drill stand-in', 'schedule': {'kind': 'once', 'run_at': str(run_at)}, 'repeat': {'times': 1, 'completed': 0},
                 'enabled': True, 'state': 'scheduled', 'deliver': 'local', 'prompt': ''})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return jid


def _alarm(life_home, key, title, kind='life_exec_failed', detail=None):
    ALARMS.append({'run': STATE['run'], 'kind': str(kind)})
    return None


def _counted(what: str, fn):
    """The engine's own check of the scheduler's records, unchanged, with its answers counted (they are the evidence
    that "is this job late, was it dropped?" was really asked of the restored records)."""
    def asked(job_id):
        got = fn(job_id)
        answer = 'could not be read' if got is None and what == 'run_records' else ('no run on record' if got == [] else
                                                                                   'still listed' if got is None else str(got) if isinstance(got, str) else 'has runs')
        if STATE['run']:
            CHECKS.setdefault(f'run {STATE["run"]}', {}).setdefault(what, {}).setdefault(answer, 0)
            CHECKS[f'run {STATE["run"]}'][what][answer] += 1
        return got
    return asked


def dress(name: str, mod) -> None:
    """Called once for each engine module, right after it loads and before any of its code is used."""
    if mod.__dict__.get('datetime') is _real_datetime:
        mod.datetime = Clock
    for attr, value in list(mod.__dict__.items()):
        if attr.startswith('__') or attr.startswith('LIVE') or not isinstance(value, (str, Path)):
            continue                           # LIVE_* names mean "the real live folder": they are compared, never opened
        try:
            if os.fspath(value) == LIVE_LIFE:
                continue
            new = _repoint(value)
        except (TypeError, ValueError):
            continue
        if new is not None:
            setattr(mod, attr, new)
    if name == 'worker':
        mod.create_job, mod._alarm, mod._job_audit = create_job, _alarm, (lambda job_id: [])
        mod._runs, mod._dropped = _counted('run_records', mod._runs), _counted('was_it_dropped', mod._dropped)
    elif name == 'noticer':
        mod.complete = _closed_model
    elif name == 'composer':
        mod._ask_zeus = lambda prompt, timeout=240: (door('a model'), None)[1]
    elif name == 'notifier':
        mod._line = lambda state_dir: Line()
    elif name == 'sources':
        mod.inbox_feed = _closed_feed
        mod.email_candidates = lambda days=2: []
        sys.modules['run_shadow'].ACCOUNTS = {k: 'closed' for k in getattr(mod, 'MAILBOX_ADDRESS', {})}
    elif name == 'pageread':
        mod.read_web = mod.read_chrome = _closed_reader
    elif name == 'writingstyle':
        mod.refresh = lambda *a, **k: {'status': 'off'}


class _Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        super().exec_module(module)
        dress(module.__name__, module)


class _EngineFinder(importlib.abc.MetaPathFinder):
    """Engine modules load through dress(); and no folder under the real home is ever searched for code except the
    engine itself (sources.py puts the live mail-helper folder on the path: it is taken off again here)."""

    @staticmethod
    def find_spec(name, path=None, target=None):
        sys.path[:] = [e for e in sys.path if not (isinstance(e, str) and _under(os.path.realpath(e or '.'), REAL_HOME)
                                                   and not any(_under(os.path.realpath(e or '.'), a) for a in CFG['allow']))]
        if path is None and '.' not in name:
            f = os.path.join(CFG['engine'], name + '.py')
            if os.path.isfile(f):
                return importlib.util.spec_from_file_location(name, f, loader=_Loader(name, f))
        return None


class _ClosedHelper(types.ModuleType):
    """A stand-in for a mail helper module the engine imports. Every UPPER-CASE name is a path inside the closed
    part of the sandbox; every other unknown name is a function that refuses. Nothing real is ever read."""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        box = Path(CFG['sandbox']) / 'closed'
        if name.isupper():
            return box / name.lower()
        return _closed_mail


def _closed_mail(*a, **k):
    door("the owner's mail")
    raise RuntimeError(f"the owner's mail is {WHY}")


def stand_ins() -> None:
    """The mail helper modules sources.py imports: small stand-ins, so no live mail helper is ever loaded or read."""
    box = Path(CFG['sandbox']) / 'closed'
    r = _ClosedHelper('run_shadow')
    r.HOME = Path(CFG['sandbox']) / 'home'
    r.OUT, r.OFF = box / 'intake', box / 'intake/intake-off'
    r.COMPOSIO, r.COMPOSIO_TMP = str(box / 'composio'), box / 'composio-tmp'
    r.ACCOUNTS = {}
    r._env = lambda: {'PATH': '/usr/bin:/bin', 'HOME': str(r.HOME)}
    r.read_message = r.to_email = _closed_mail
    r.candidates = lambda days=2: []
    li = _ClosedHelper('life_intake')
    li.PACIFIC = ZoneInfo(OWNER_TZ)
    li.html_to_text = lambda s: re.sub(r'<[^>]+>', ' ', str(s or ''))
    sys.modules['run_shadow'], sys.modules['life_intake'] = r, li


# ---- what the restored ledger says ---------------------------------------------------------------------------------------

def ledger_rows() -> list:
    with closing(sqlite3.connect(str(Path(CFG['life']) / 'ledger.sqlite3'), timeout=10)) as con:
        return con.execute('SELECT key, state, autonomy, surface_at, payload FROM opps').fetchall()


def snapshot(mine: set) -> dict:
    """key -> its state, and the job it holds if that job is one this drill booked."""
    out = {}
    for key, state, _autonomy, _due, payload in ledger_rows():
        try:
            ex = (json.loads(payload) or {}).get('exec') or {}
        except (ValueError, AttributeError):
            ex = {}
        jid = str(ex.get('job_id') or '')
        out[key] = {'state': state, 'job': jid if jid in mine else '', 'tries': ex.get('tries')}
    return out


def run_tick(T, at: float) -> dict:
    set_clock(at)
    STATE['run'] += 1
    here = _real_datetime.fromtimestamp(at).astimezone()
    info = {'at': here.isoformat(timespec='seconds')}
    sys.argv = [sys.argv[0], '--max-calls', '12', '--email-days', '2']      # the engine reads only what follows the name
    out, err = io.StringIO(), io.StringIO()
    last = Path(CFG['life']) / 'last-tick.json'
    before = last.stat().st_mtime_ns if last.exists() else None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            T.main()
    except SystemExit as e:
        info['exit'] = e.code
    except BaseException as e:      # noqa: BLE001  (a run that stops is reported, never hidden)
        info['stopped_by'] = f'{type(e).__name__}: {str(e)[:160]}'
    try:
        info['said'] = json.loads(out.getvalue().strip().splitlines()[-1])
    except (ValueError, IndexError):
        info['said'] = None
    report = None
    if last.exists() and last.stat().st_mtime_ns != before:
        try:
            report = json.loads(last.read_text())
        except ValueError:
            report = None
    errors = [str(e) for e in (report or {}).get('errors') or []]
    info['crashed'] = [e[:200] for e in errors if re.match(r'^[a-z_]+: [A-Za-z_.]+(Error|Exception|Exit|Interrupt)\b', e)]
    info['other_errors'] = len(errors) - len(info['crashed'])
    info['work'] = (report or {}).get('work')
    info['delivery'] = {k: v for k, v in ((report or {}).get('delivery') or {}).items() if isinstance(v, (int, bool))}
    info['mode'] = (report or {}).get('mode')
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--sandbox', required=True)
    ap.add_argument('--engine', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--now', default=None)
    ap.add_argument('--live-profile', default=None, help='the live profile folder: paths under it are pointed into the sandbox')
    ap.add_argument('--owner-tz', default=None, help="the owner's home time zone (IANA name)")
    a = ap.parse_args()
    global LIVE_PROFILE, LIVE_LIFE, OWNER_TZ
    if a.live_profile:
        LIVE_PROFILE = os.path.realpath(a.live_profile)
        LIVE_LIFE = LIVE_PROFILE + '/workspace/life-engine'
    if a.owner_tz:
        try:
            ZoneInfo(a.owner_tz)
            OWNER_TZ = a.owner_tz
        except Exception:      # noqa: BLE001  (a bad zone name keeps UTC)
            pass
    sandbox, engine = os.path.realpath(a.sandbox), os.path.realpath(a.engine)
    profile = sandbox + '/profile'
    life = profile + '/workspace/life-engine'
    CFG.update(sandbox=sandbox, engine=engine, profile=profile, life=life,
               allow=tuple(dict.fromkeys(os.path.realpath(p) for p in (sandbox, engine, HERE, sys.base_prefix, sys.prefix, sys.exec_prefix))),
               writable=tuple(dict.fromkeys([sandbox, os.path.realpath(tempfile.gettempdir()), '/private/tmp', '/tmp', '/private/var/folders',
                                             '/var/folders', '/dev'])))
    result = {'python': sys.version.split()[0], 'engine': engine}

    def finish(code: int) -> int:
        result.update(sends=len(SENDS), alarms=len(ALARMS), alarm_kinds=sorted({x['kind'] for x in ALARMS}), doors=DOORS, door_examples=DOOR_EXAMPLES,
                      scheduler_checks=CHECKS)
        Path(a.out).write_text(json.dumps(result, indent=1, default=str))
        return code

    if os.path.realpath(life) == os.path.realpath(LIVE_LIFE) or _under(sandbox, REAL_HOME + '/.hermes'):
        result['error'] = 'refused: the folder given is the live one'
        return finish(2)
    result['live_switch_present'] = os.path.exists(life + '/notify-live')
    if result['live_switch_present']:
        result['error'] = 'refused: the restored life folder has the live-sending switch'
        return finish(2)

    # the environment, before any engine code loads
    for k in [k for k in os.environ if k.startswith(('LIFE_', 'HERMES_', 'CLAUDE_', 'Zeus_'))]:
        del os.environ[k]
    os.makedirs(sandbox + '/home', exist_ok=True)
    chat = profile + '/state.db'
    os.environ.update(HOME=sandbox + '/home', LIFE_HOME=life, LIFE_ENGINE_DIR=engine, LIFE_ENGINE=engine,
                      LIFE_CHAT_DB=chat if os.path.isfile(chat) else sandbox + '/closed/no-chat-store.db',
                      LIFE_VOICE_EXAMPLES=life + '/voice-examples.json', LIFE_TEAM_FILE=sandbox + '/closed/no-team.json',
                      LIFE_MEMORY_MD=sandbox + '/closed/no-memory.md', LIFE_JOBS_FILE=profile + '/cron/jobs.json',
                      CLAUDE_BIN=sandbox + '/closed/claude', LIFE_CHROME_CDP='http://127.0.0.1:9', LIFE_INTAKE_HOME=sandbox + '/closed/intake')
    base = _real_datetime.fromisoformat(a.now).timestamp() if a.now else _real_time()
    set_clock(base)
    time.time = drill_time
    close_doors()
    stand_ins()
    sys.meta_path.insert(0, _EngineFinder)
    sys.path.insert(0, engine)

    try:
        import tick as T
    except BaseException as e:      # noqa: BLE001
        result['error'] = f'the engine could not be loaded: {type(e).__name__}: {str(e)[:200]}'
        return finish(1)
    if os.path.realpath(str(T.LIFE)) != os.path.realpath(life):
        result['error'] = 'refused: the engine is not looking at the restored folder'
        return finish(2)

    switches = sorted(p for p in os.listdir(life) if '.' not in p and os.path.isfile(os.path.join(life, p)))
    try:
        mandate = json.loads(Path(life, 'mandate.json').read_text())
    except (OSError, ValueError):
        mandate = {}
    now_iso = Clock.now(getattr(sys.modules.get('policy'), 'PACIFIC', ZoneInfo(OWNER_TZ))).isoformat()
    rows = ledger_rows()
    result.update(
        switches=switches, stop_file='off' in switches,
        can_work=bool(mandate.get('active')) and 'executor-off' not in switches,
        due_before=sorted(k for k, s, au, due, _p in rows if s == 'pending' and au in ('auto', 'offer', 'ask') and str(due or '') <= now_iso),
        in_flight_before=sorted(k for k, s, _au, _due, _p in rows if s == 'executing'),
        open_before=sum(1 for _k, s, *_ in rows if s in OPEN),
        drill_clock=[], runs=[])

    after = []
    for i in range(2):
        at = base + i * SECOND_RUN_AFTER
        result['drill_clock'].append(_real_datetime.fromtimestamp(at).astimezone().isoformat(timespec='seconds'))
        result['runs'].append(run_tick(T, at))
        after.append(snapshot({j['id'] for j in JOBS}))
    per_item = {}
    for snap in after:
        for key, row in snap.items():
            if row['job']:
                per_item.setdefault(key, [])
                if row['job'] not in per_item[key]:
                    per_item[key].append(row['job'])
    owned = {j for ids in per_item.values() for j in ids}
    result.update(jobs=JOBS, jobs_per_item=per_item, jobs_without_item=sorted(j['id'] for j in JOBS if j['id'] not in owned),
                  after_run_1={k: v for k, v in after[0].items() if k in set(result['due_before']) | set(result['in_flight_before']) | set(per_item)},
                  after_run_2={k: v for k, v in after[1].items() if k in set(result['due_before']) | set(result['in_flight_before']) | set(per_item)})
    return finish(0)


if __name__ == '__main__':
    sys.exit(main())
