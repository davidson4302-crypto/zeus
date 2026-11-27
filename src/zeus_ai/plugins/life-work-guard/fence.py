"""Effect fence: an action that was cut off is never done again on its own.

Every action that changes something is written down BEFORE it starts, and an action that was interrupted (a crash, a
restart) is not run again by itself. Zeus's own send, booking, purchase and approval paths already do this, each in
its own ledger. This covers the remaining case, a chat turn that a gateway restart cut off: Hermes
resumes it with a fresh model turn, and the only thing stopping it from sending, booking or changing the same thing
again is a sentence in the resume note ("Do NOT re-execute old tool calls"). A repeat gets a new tool-call id, so the
delivery journal treats it as a new send.

This fence works on the action's CONTENT, not its id:
  before   every tool call that changes something outside (the engine audit's own classify(): kind "write", minus
           the owner's memory and Zeus's own work list) gets a row: fingerprint of the tool and its exact arguments, the
           process that started it, the time. Written and committed before the tool runs.
  after    post_tool_call closes the row (done, or removed when another check blocked the call before it ran).
  repeat   the same fingerprint again while an earlier row is still open:
             - the process that started it is gone (restart, crash): REFUSED. It may already have happened. Zeus is
               told to look where it would have landed, and how to release it once it has checked.
             - the same process is still running it: REFUSED as "already running".
           A finished row never blocks: doing the same thing again on purpose works as before.
Open rows older than WINDOW are ignored and tidied. Any error inside the fence lets the call through (the paths'
own ledgers still hold); the fence only ever removes a repeat.

Switch file in the life folder: effect-fence-on (without it nothing here runs).
CLI:  python3 fence.py list | release <id> --checked "<what you looked at>"
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SWITCH = 'effect-fence-on'
DB = 'effect-fence.sqlite3'
WINDOW = 24 * 3600
STILL_RUNNING = 15 * 60             # an open row of a LIVE process older than this lost its closing; it never blocks
SKIP_DETAIL = ('memory ',)          # the owner's memory: saving the same note twice is harmless and memory has its own checks
LOG = 'effect-fence.log.jsonl'

INTERRUPTED = ('Not run: this exact action ({what}) was started at {when} and was cut off before it finished (most '
               'likely a restart), so it may already have happened. Do not try it again yet. First look where it would '
               'have landed (the chat or thread, the owner\'s sent mail, the calendar, the order page). If it is there, it is '
               'done: say so. If you can see it truly did not happen, release it with this command and then do it '
               'once: python3 -I -B {me} release {id} --checked "<what you looked at>"')
RUNNING = ('Not run: this exact action ({what}) is already running right now from an earlier step. Wait for that one '
           'to finish; do not start it a second time.')


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _boot_id() -> str:
    """This process's identity: pid plus its start time, so a reused pid after a restart is not mistaken for it."""
    return f'{os.getpid()}:{int(_START)}'


_START = time.time()


def fingerprint(tool_name, args) -> str:
    name = str(tool_name or '').split('__')[-1]
    body = json.dumps(args if isinstance(args, dict) else {'_': str(args)}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256((name + '\n' + body).encode()).hexdigest()[:32]


def _con(life: Path):
    con = sqlite3.connect(str(Path(life) / DB), timeout=10, isolation_level=None)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY, fp TEXT, tool TEXT, what TEXT, session TEXT,'
                ' owner TEXT, pid INTEGER, started REAL, finished REAL, status TEXT, released TEXT)')
    con.execute('CREATE INDEX IF NOT EXISTS effects_fp ON effects(fp, finished)')
    return con


def on(life) -> bool:
    return (Path(life) / SWITCH).exists()


def _log(life, row):
    try:
        with open(Path(life) / LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'at': time.time(), **row}, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


def _when(ts) -> str:
    try:
        return time.strftime('%-I:%M%p %b %-d', time.localtime(float(ts))).replace('AM', 'am').replace('PM', 'pm')
    except Exception:
        return 'an earlier time'


def before(life, tool_name, args, detail: str, session_id='', tool_call_id='', now=None):
    """-> None (go ahead; the row is written) or a block message. `detail` is the audit's description of the write."""
    if str(detail or '').startswith(SKIP_DETAIL):
        return None
    now = time.time() if now is None else now
    fp = fingerprint(tool_name, args)
    me = _boot_id()
    con = _con(life)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.execute('DELETE FROM effects WHERE started < ?', (now - 7 * 86400,))
        open_rows = con.execute('SELECT id, owner, pid, started, what FROM effects WHERE fp=? AND finished IS NULL AND released IS NULL'
                                ' AND started >= ? ORDER BY started', (fp, now - WINDOW)).fetchall()
        for rid, owner, pid, started, what in open_rows:
            live_elsewhere = int(pid or 0) != os.getpid() and _pid_alive(pid)
            if owner == me or live_elsewhere:
                if now - float(started) > STILL_RUNNING:
                    continue                    # no tool call runs this long: its closing was lost, not a cut-off
                con.execute('COMMIT')
                _log(life, {'verdict': 'already running', 'id': rid, 'tool': str(tool_name)[:80]})
                return RUNNING.format(what=what)
            con.execute('COMMIT')               # its process is gone (or this pid was reused after a restart): cut off
            _log(life, {'verdict': 'interrupted repeat refused', 'id': rid, 'tool': str(tool_name)[:80], 'started': started})
            return INTERRUPTED.format(what=what, when=_when(started), me=Path(__file__).resolve(), id=rid)
        rid = 'fx-' + hashlib.sha256(f'{fp}{now}{tool_call_id}'.encode()).hexdigest()[:10]
        con.execute('INSERT INTO effects(id, fp, tool, what, session, owner, pid, started, status) VALUES(?,?,?,?,?,?,?,?,?)',
                    (rid, fp, str(tool_name)[:120], str(detail or tool_name)[:160], str(session_id)[:120], me, os.getpid(), now, 'started'))
        con.execute('COMMIT')
        _pending[_key(tool_name, args, session_id, tool_call_id)] = rid
        return None
    finally:
        con.close()


_pending: dict = {}


def _key(tool_name, args, session_id, tool_call_id) -> str:
    """The call's own id when Hermes gives one (another plugin may change the arguments between before and after)."""
    if tool_call_id:
        return f'{session_id}|{tool_call_id}'
    return f'{session_id}||{fingerprint(tool_name, args)}'


def after(life, tool_name, args, session_id='', tool_call_id='', status=None, now=None) -> None:
    """Close this call's row: removed when it was blocked before running, else marked finished with its status."""
    rid = _pending.pop(_key(tool_name, args, session_id, tool_call_id), None)
    if not rid:
        return
    now = time.time() if now is None else now
    con = _con(life)
    try:
        if status == 'blocked':
            con.execute('DELETE FROM effects WHERE id=?', (rid,))
        else:
            con.execute('UPDATE effects SET finished=?, status=? WHERE id=?', (now, str(status or 'done')[:40], rid))
    finally:
        con.close()


def release(life, rid: str, checked: str) -> str:
    checked = ' '.join(str(checked or '').split())
    if len(checked) < 8:
        return 'Not released: say in a few words what you looked at to see it did not happen.'
    con = _con(life)
    try:
        n = con.execute('UPDATE effects SET released=? WHERE id=? AND finished IS NULL AND released IS NULL',
                        (json.dumps({'at': time.time(), 'checked': checked[:300]}), rid)).rowcount
    finally:
        con.close()
    _log(life, {'verdict': 'released', 'id': rid, 'checked': checked[:300], 'ok': bool(n)})
    return f'Released {rid}. You may do it once now.' if n else f'No open action {rid} to release.'


def list_open(life) -> list:
    con = _con(life)
    try:
        return con.execute('SELECT id, what, started, owner FROM effects WHERE finished IS NULL AND released IS NULL'
                           ' AND started >= ? ORDER BY started', (time.time() - WINDOW,)).fetchall()
    finally:
        con.close()


def _default_profile() -> Path:
    """The Hermes profile folder from engine/settings.py; the default profile layout if it cannot be loaded."""
    try:
        engine = Path(__file__).resolve().parents[2] / 'engine'
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        import settings
        return Path(settings.PROFILE_DIR)
    except Exception:
        return Path.home() / '.hermes' / 'profiles' / 'zeus'


def _cli(argv) -> int:
    life = Path(os.environ.get('LIFE_HOME') or (Path(os.environ.get('HERMES_HOME') or _default_profile()) / 'workspace/life-engine'))
    if argv[:1] == ['list']:
        rows = list_open(life)
        print('\n'.join(f'{r[0]}  {r[1]}  started {_when(r[2])}' for r in rows) or 'No cut-off actions.')
        return 0
    if argv[:1] == ['release'] and len(argv) >= 2:
        checked = argv[argv.index('--checked') + 1] if '--checked' in argv and argv.index('--checked') + 1 < len(argv) else ''
        print(release(life, argv[1], checked))
        return 0
    print(__doc__.split('CLI:')[1].strip())
    return 2


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
