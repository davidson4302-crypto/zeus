"""capability: what Zeus can and cannot use right now, one small answer per thing.

Why it exists: each check lives where it is made (the mail feed and the calendar copy in the health note, a
store's sign-in in signins.json, a page read in watches.json, the texting line in the outbox, a job's records in the
scheduler's database), and this module puts them side by side, so a job that needs a store is told the store is signed
out, and a note written while the mail feed is down never reads as if nothing had come in.

status(life, name, now) and all(life, now) answer with
  {'name', 'status', 'checked_at', 'target', 'evidence', 'recovery'}
status is one of
  available            checked, and it worked
  permission_required  only the owner can open it (a code the store sends them, sending switched off, no saved login)
  signed_out           a store's account page showed its sign-in page
  stale                there is no fresh proof either way (never checked, or the last check is old)
  broken               checked, and it did not work; or it cannot be proven to be what it should be
  unsupported          this folder cannot check it (a sandbox), or the thing is not set up

These are thin wrappers. Nothing here reads a page, signs in, sends or probes a model: every answer is worked out from
what the existing checks already wrote down (lifehealth.py's note, signedin.py's and watches.py's files, the engine's
outbox, the scheduler's own records), with ONE exception that has no other check: whose browser is on port 9333.

Zeus's own Chrome (pageread.py, port 9333): it must be Zeus's own browser instance and its assigned profile. Code can
see that from the process that listens on the control port: its data folder and its profile flag. When that process
cannot be seen, or shows another data folder or profile, identity is NOT established: the status is `broken`, every
signed-in read is refused before it is made (read_blocked, asked by watches.read, which the watches, the monitors, the
arm step and the signed-in check all go through), public reads carry on, and nothing ever falls back to the owner's own
Chrome (no code path here or in pageread.py names another browser). The probe runs only from the live life folder
(the same door watches.readers uses), at most every five minutes; anywhere else, without a stand-in, the status is
`unsupported` and nothing is run.

Also here, because it is the same rule ("could not read" is never "nothing there"): after_notice writes down which
source this run's noticing pass could not read and since when, and composing hands the note writer that fact, so a
note is never written as if a source that was down had been empty.

Switch file in the life folder: `capability-on`. Without it every hook hands back exactly what it was given, the health
note is what it was, and nothing is written. status() and all() themselves are plain reads and work with or without it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import settings

SWITCH = 'capability-on'
FILE = 'capability.json'           # in the life folder: the browser check's last answer, and what could not be read since when
STATUSES = ('available', 'permission_required', 'signed_out', 'stale', 'broken', 'unsupported')
BASE = ('inbox_feed', 'calendar_copy', 'page_reader', 'own_chrome', 'texting_line', 'job_scheduler')
HEALTH_OLD = 20 * 60               # a health note older than this proves nothing about now (the tick writes one every five minutes)
READ_OLD = 24 * 3600               # a good page read or sign-in reading older than this is no proof for today
CHROME_FRESH = 5 * 60              # the browser's identity is looked at again after this long
CHROME_DATA_DIR = os.environ.get('LIFE_CHROME_DATA_DIR', str(settings.CHROME_DATA_DIR))   # Zeus's own Chrome keeps its data here
CHROME_PROFILE = os.environ.get('LIFE_CHROME_PROFILE', 'Default')                                       # and this is the profile assigned to Zeus
CHROME_PROBE = None                # tests: () -> {'reachable': bool, 'command': the listening process's command line or None}
RECORDS = None                     # tests: {'exec_db': path, 'jobs': path} in place of the scheduler's real records
CLOCK = None                       # tests: () -> the moment it is, for the one caller that is not handed a time (read_blocked)
BLOCKED = "Zeus's own Chrome could not be confirmed as its own browser and profile, so nothing signed-in was read"
UNREAD_RULE = ('\n- An item may carry "also_could_not_be_read". That is not a thing to report on by itself: it says which of your own sources you '
               'could not read, and for how long. If your message could be taken to mean nothing else came in, say in one short clause that you '
               'could not read that source for that time. Never write as if nothing came in.')
UNREAD_WHY = 'this note reads as if everything was looked at; say in one short clause, in your own words: {fact}'
_NOT_READ = re.compile(r"could ?n[o']t|couldn.?t|can.?t|cannot|unable|not (?:been )?(?:able|readable|read)|hasn.?t|haven.?t|was down|is down|been down|unread|not reach", re.I)


def on(life) -> bool:
    try:
        return life is not None   # always on: the capability-on switch is folded in
    except (OSError, TypeError):
        return False


def _tz():
    try:
        from ownertime import PACIFIC
        return PACIFIC
    except Exception:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings.OWNER_TZ)


def _dt(value=None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_tz())
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), _tz())
    return CLOCK() if CLOCK is not None else datetime.now(_tz())


def _moment(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00')) if value else None
    except ValueError:
        return None
    return None if t is None else (t if t.tzinfo else t.replace(tzinfo=_tz()))


def _iso(dt: datetime) -> str:
    return dt.astimezone(_tz()).isoformat(timespec='seconds')


def _one(text, limit: int = 240) -> str:
    return ' '.join(str(text or '').split())[:limit]


def _when(value, now: datetime) -> str:
    """'today at 2:06pm' / 'Sat Nov 14 at 2:06pm'."""
    t = _moment(value)
    if t is None:
        return 'an unknown time'
    t = t.astimezone(_tz())
    hour = t.strftime('%I:%M%p').lstrip('0').lower()
    return ('today at ' if t.date() == now.astimezone(_tz()).date() else t.strftime('%a %b %d at ').replace(' 0', ' ')) + hour


def _day(value, now: datetime) -> str:
    t = _moment(value)
    if t is None:
        return 'earlier'
    t = t.astimezone(_tz())
    return 'today' if t.date() == now.astimezone(_tz()).date() else t.strftime('on %a %b %d').replace(' 0', ' ')


def _rec(name, status, checked_at, target, evidence, recovery='') -> dict:
    return {'name': name, 'status': status if status in STATUSES else 'broken', 'checked_at': checked_at, 'target': target,
            'evidence': _one(evidence), 'recovery': _one(recovery)}


def _live(life) -> bool:
    """The live life folder, or a rehearsal on purpose: the only places a real probe may run (watches.readers' own door)."""
    try:
        import watches as W
        return Path(life).resolve() == W.LIVE_HOME.resolve() or os.environ.get('LIFE_WATCH_REAL_READS') == '1'
    except Exception:
        return False


# ---- the small state file ------------------------------------------------------------------------------------------------

def _load(life) -> dict:
    try:
        d = json.loads((Path(life) / FILE).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(life, d: dict) -> None:
    life = Path(life)
    tmp = life / f'.{FILE}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w') as fh:
            fh.write(json.dumps(d, indent=1, ensure_ascii=False, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, life / FILE)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---- the wrapped checks --------------------------------------------------------------------------------------------------

def _health(life, now: datetime, note=None):
    """The health note to judge from: the one being built (tick.health_note hands it over) or the last one written.
    -> (note, how old in seconds, or None when there is none)."""
    if isinstance(note, dict):
        return note, 0
    try:
        h = __import__('lifehealth').read(life)
    except Exception:
        h = {}
    if not h:
        return {}, None
    try:
        age = max(0, now.timestamp() - float(h.get('when_ts')))
    except (TypeError, ValueError):
        age = None
    return h, age


def _from_health(name, part, target, life, now, note):
    h, age = _health(life, now, note)
    if not h or age is None:
        return None, _rec(name, 'stale', None, target, 'no health note has been written in this folder yet, so there is no check to go on')
    if age > HEALTH_OLD:
        return None, _rec(name, 'stale', h.get('when'), target, f'the last health note is {round(age / 60)} minutes old, so it proves nothing about now',
                          'the 5-minute run writes this note; if it stays old the run itself is not running')
    got = h.get(part)
    if not isinstance(got, dict):
        return None, _rec(name, 'stale', h.get('when'), target, 'the health note has no reading for this')
    return (h, got), None


def inbox_feed(life, now=None, note=None) -> dict:
    """The mail feed (the inbox watch list), as lifehealth.inbox_state judged it."""
    now, target = _dt(now), "the inbox watch's list of the owner's new mail"
    got, early = _from_health('inbox_feed', 'inbox_feed', target, life, now, note)
    if early:
        return early
    h, box = got
    fix = 'mail that came in meanwhile is unread, not absent; one repair ticket a day is raised when health-alarms-on is present'
    if box.get('readable') is False:
        return _rec('inbox_feed', 'broken', h.get('when'), target, 'the feed could not be read at all', fix)
    if box.get('stale') or box.get('ok') is False:
        return _rec('inbox_feed', 'stale', h.get('when'), target,
                    f"no successful mailbox check since {_when(box.get('stale_since'), now)}" if box.get('stale_since') else 'a mailbox has no recent successful check', fix)
    return _rec('inbox_feed', 'available', h.get('when'), target, 'every mailbox was checked within the last 30 minutes')


def calendar_copy(life, now=None, note=None) -> dict:
    """The calendar copy (the configured calendar snapshot), as lifehealth.calendar_state judged it."""
    now, target = _dt(now), "the hourly copy of the owner's calendars"
    got, early = _from_health('calendar_copy', 'calendar', target, life, now, note)
    if early:
        return early
    h, cal = got
    fix = 'a gap on a calendar that could not be read is unknown, not free'
    unreadable = [u for u in cal.get('unreadable') or [] if isinstance(u, dict)]
    if any(u.get('stage') == 'file' for u in unreadable):
        return _rec('calendar_copy', 'broken', h.get('when'), target, 'the calendar copy could not be opened', fix)
    if cal.get('stale'):
        return _rec('calendar_copy', 'stale', cal.get('checked_at') or h.get('when'), target,
                    f"the copy was last refreshed {_when(cal.get('checked_at'), now)} and has missed its last two hourly runs", fix)
    if unreadable or cal.get('ok') is False:
        names = ', '.join(dict.fromkeys(str(u.get('name')) for u in unreadable if u.get('name'))) or 'one of the calendar accounts'
        return _rec('calendar_copy', 'broken', cal.get('checked_at') or h.get('when'), target, f'could not be read: {names}', fix)
    return _rec('calendar_copy', 'available', cal.get('checked_at') or h.get('when'), target, 'every calendar was read and the copy is fresh')


def page_reader(life, now=None) -> dict:
    """The public page reader (pageread.read_web), judged by what the watches' own reads of public pages last did."""
    now, target = _dt(now), 'public web pages, by plain GET'
    try:
        import watches as W
        if W.readers(life).get('web') is W._off_reader:
            return _rec('page_reader', 'unsupported', None, target, 'reads are switched off outside the live folder')
        d = W.Store(life).load()
    except Exception as e:
        return _rec('page_reader', 'broken', None, target, f'the list of watches could not be looked at ({type(e).__name__})')
    if d is None:
        return _rec('page_reader', 'broken', None, target, 'the list of watches could not be read')
    web = [w for w in d['watches'] if (w.get('read') or {}).get('kind') == 'web' and _moment(w.get('checked_at'))]
    recent = [w for w in web if (now - _moment(w['checked_at'])).total_seconds() <= READ_OLD]
    good = [w for w in recent if not int(w.get('fails') or 0)]
    if good:
        newest = max(good, key=lambda w: _moment(w['checked_at']))
        return _rec('page_reader', 'available', newest['checked_at'], target, f"a public page was read {_when(newest['checked_at'], now)}")
    if recent:
        newest = max(recent, key=lambda w: _moment(w['checked_at']))
        return _rec('page_reader', 'broken', newest['checked_at'], target,
                    f"every public page read in the last day failed ({len(recent)} watched, the last try {_when(newest['checked_at'], now)})",
                    'each page is tried again on its own timer; a page that cannot be read is unknown, never unchanged')
    return _rec('page_reader', 'stale', None, target, 'no public page has been read in the last day, so there is no proof either way')


def _real_probe() -> dict:
    """Who is on the control port? Reachable: the port answers its own version request. Command: the command line of
    the process listening there (lsof, then ps). Read-only, this Mac only. Never run from a test or a sandbox."""
    import pageread
    host, port = pageread._local(pageread.CDP)
    reachable, command = False, None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(f'http://{host}:{port}/json/version', method='GET'), timeout=3) as r:
            reachable = bool(json.loads(r.read(200_000)).get('webSocketDebuggerUrl'))
    except Exception:
        reachable = False
    try:
        pids = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'], capture_output=True, text=True, timeout=5).stdout.split()
        if pids:
            command = subprocess.run(['/bin/ps', '-ww', '-o', 'command=', '-p', pids[0]], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        command = None
    return {'reachable': reachable, 'command': command, 'port': port}


def _has_flag(command: str, flag: str, value: str) -> bool:
    return bool(re.search(r'(?:^|\s)' + re.escape(f'{flag}={value}') + r'(?=\s|$)', command)) or f'{flag}="{value}"' in command


def identity(probe: dict) -> tuple:
    """-> ('confirmed' | 'wrong' | 'not established' | 'unreachable', why). Confirmed needs the listening process's own
    command line to show Zeus's data folder AND its assigned profile. Not seeing the process is never "confirmed"."""
    if not isinstance(probe, dict) or not probe.get('reachable'):
        return 'unreachable', "Zeus's own Chrome did not answer on its control port"
    command = str(probe.get('command') or '')
    if not command.strip():
        return 'not established', 'the browser answering on the control port could not be identified (its process could not be seen)'
    if not _has_flag(command, '--user-data-dir', CHROME_DATA_DIR):
        return 'wrong', "the browser answering on the control port is not running from Zeus's own data folder"
    if not _has_flag(command, '--profile-directory', CHROME_PROFILE):
        return 'wrong', "the browser answering on the control port was not started in Zeus's assigned profile"
    return 'confirmed', "the browser on the control port runs from Zeus's own data folder, in its assigned profile"


def _chrome(life, now: datetime):
    """-> ('confirmed' | 'wrong' | 'not established' | 'unreachable' | 'unchecked', why, checked_at). Kept for CHROME_FRESH."""
    if CHROME_PROBE is None and not _live(life):
        return 'unchecked', 'the browser is not looked at outside the live folder', None
    d = _load(life)
    kept = d.get('chrome') if isinstance(d.get('chrome'), dict) else {}
    at = _moment(kept.get('at'))
    if at is not None and 0 <= (now - at).total_seconds() < CHROME_FRESH and kept.get('identity'):
        return kept['identity'], kept.get('why') or '', kept.get('at')
    try:
        probe = (CHROME_PROBE or _real_probe)()
    except Exception as e:
        probe = {'reachable': False, 'error': type(e).__name__}
    who, why = identity(probe)
    d['chrome'] = {'at': _iso(now), 'identity': who, 'why': why}
    try:
        _save(life, d)
    except OSError:
        pass
    return who, why, _iso(now)


def own_chrome(life, now=None) -> dict:
    """Zeus's own Chrome: reachable, and proven to be its own browser instance and assigned profile."""
    now, target = _dt(now), "Zeus's own Chrome on its local control port (signed-in pages)"
    who, why, at = _chrome(life, now)
    stop = "signed-in reads and sign-ins are stopped until it is proven; public reads go on; the owner's own Chrome is never used instead"
    if who == 'unchecked':
        return _rec('own_chrome', 'unsupported', None, target, why)
    if who == 'confirmed':
        return _rec('own_chrome', 'available', at, target, why)
    if who == 'unreachable':
        return _rec('own_chrome', 'broken', at, target, why, "anything that needs a signed-in page waits; the owner's own Chrome is never used instead")
    return _rec('own_chrome', 'broken', at, target, why, stop)


def read_blocked(life, kind) -> str:
    """watches.read, before a read is made. A signed-in read ('chrome') is refused when the browser on the control port
    is reachable but is not proven to be Zeus's own instance and profile: -> the plain reason. '' for a public read, for
    a browser that is confirmed, unreachable (the read fails by itself) or not checked here, and without the switch."""
    try:
        if kind != 'chrome' or not on(life):
            return ''
        who, _why, _at = _chrome(life, _dt())
        return BLOCKED if who in ('wrong', 'not established') else ''
    except Exception:
        return ''


def _relogin_stop(life, site: str, now: datetime):
    try:
        import recover as R
        return R._relogin_stop(life, site, now)
    except Exception:
        return None


def store(life, site: str, now=None) -> dict:
    """One signed-in store, as signedin.py last read it (signins.json) and, for a lapse, how Zeus's own try at signing
    back in ended (relogin.jsonl)."""
    now = _dt(now)
    name_id = f'store:{site}'
    try:
        import signedin as S
        entry = S.sites(life).get(site)
        if entry is None:
            return _rec(name_id, 'unsupported', None, site, 'this is not one of the sites in the sign-in table')
        name = str(entry.get('name') or site)
        target = f"{name}, signed in, in Zeus's own Chrome"
        if not S.usable(entry):
            return _rec(name_id, 'unsupported', None, target, 'this site is not set up yet: its sign-in pages have not been captured')
        s = S._state(life, site)
    except Exception as e:
        return _rec(name_id, 'broken', None, site, f'the sign-in notes could not be looked at ({type(e).__name__})')
    if not s:
        return _rec(name_id, 'stale', None, target, 'this sign-in has never been checked')
    lapse = s.get('lapse')
    if lapse:
        asked = f"the owner was asked to sign in {_day(lapse.get('asked_at'), now)}" if lapse.get('ask_key') and lapse.get('ask_key') != 'muted' else \
            'the owner said to stop raising this, so they were not asked' if lapse.get('ask_key') == 'muted' else 'the owner has not been asked yet'
        stop = _relogin_stop(life, site, now)
        if stop:
            import recover as R
            return _rec(name_id, 'permission_required', s.get('checked_at'), target,
                        f"signed out since {_when(lapse.get('since'), now)}; its own try at signing back in stopped: {R.ACCESS_STOPS[stop]}",
                        f'only the owner can finish this sign-in; {asked}')
        return _rec(name_id, 'signed_out', s.get('checked_at'), target, f"its account page has shown the sign-in page since {_when(lapse.get('since'), now)}",
                    f'{asked}; what waits on it carries on by itself once a read shows it signed in')
    checked = _moment(s.get('checked_at'))
    if s.get('status') == 'signed_in':
        if checked is not None and (now - checked).total_seconds() <= READ_OLD:
            return _rec(name_id, 'available', s.get('checked_at'), target, f"its account page showed a signed-in page {_when(s.get('checked_at'), now)}")
        return _rec(name_id, 'stale', s.get('checked_at'), target, 'the last signed-in reading is more than a day old')
    if int(s.get('unknown_checks') or 0) >= 2:
        return _rec(name_id, 'broken', s.get('checked_at'), target, f"its account page could not be read {s.get('unknown_checks')} times in a row",
                    'not being able to tell is never treated as signed out: nobody is asked and nothing is held')
    return _rec(name_id, 'stale', s.get('checked_at'), target, 'the last look could not tell whether it is signed in')


def texting_line(life, now=None) -> dict:
    """The line the owner's notes go out on: is sending switched on, and what did the last real send end as (the engine's own
    outbox; the words of a note are never read here)."""
    now, target = _dt(now), "the owner's iMessage, through the texting line"
    life = Path(life)
    if not (life / 'notify-live').exists():
        return _rec('texting_line', 'permission_required', None, target, 'sending is switched off for this folder: notes are written and held, nothing goes out',
                    'it goes out when the owner switches sending on (the notify-live file)')
    db = life / 'ledger.sqlite3'
    if not db.exists():
        return _rec('texting_line', 'stale', None, target, 'nothing has been sent from this folder yet, so there is no proof either way')
    try:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            rows = [tuple(r) for r in con.execute("SELECT state, at FROM outbox WHERE mode='live' ORDER BY at DESC LIMIT 3")]
    except sqlite3.Error as e:
        return _rec('texting_line', 'broken', None, target, f'the record of what was sent could not be read ({type(e).__name__})')
    if not rows:
        return _rec('texting_line', 'stale', None, target, 'nothing has been sent from this folder yet, so there is no proof either way')
    state, at = rows[0]
    when = _iso(_dt(float(at))) if at else None
    if state == 'sent':
        return _rec('texting_line', 'available', when, target, f'the last note went out {_when(when, now)} with a receipt')
    if state == 'failed' and len(rows) > 1 and rows[1][0] == 'failed':
        return _rec('texting_line', 'broken', when, target, f'the last two notes failed to send, the latest {_when(when, now)}',
                    'the items stay ready and go with the next note that sends; nothing is marked as told')
    return _rec('texting_line', 'stale', when, target,
                f"the last note ({_when(when, now)}) has no receipt yet" if state != 'failed' else f'the last note failed to send {_when(when, now)}',
                "a send with no receipt is settled by reading the owner's messages back, never by sending again")


def job_scheduler(life, now=None) -> dict:
    """The job scheduler's records (the run records worker._runs reads, and its job list): can they be read?"""
    now, target = _dt(now), "the scheduler's run records and job list"
    if RECORDS is None and not _live(life):
        return _rec('job_scheduler', 'unsupported', None, target, "the scheduler's records are not looked at outside the live folder")
    try:
        import worker as Wk
        exec_db = Path((RECORDS or {}).get('exec_db') or Wk.EXEC_DB)
        jobs = Path((RECORDS or {}).get('jobs') or Wk.JOBS_FILE)
    except Exception as e:
        return _rec('job_scheduler', 'broken', None, target, f'the worker could not be loaded ({type(e).__name__})')
    fix = 'a job whose records cannot be read is never requeued on a guess: its records are read first'
    try:
        with closing(sqlite3.connect(exec_db.as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            runs = con.execute('SELECT COUNT(*) FROM executions').fetchone()[0]
    except (sqlite3.Error, OSError) as e:
        return _rec('job_scheduler', 'broken', _iso(now), target, f'the run records could not be read ({type(e).__name__})', fix)
    try:
        data = json.loads(jobs.read_text())
        listed = data.get('jobs', data) if isinstance(data, dict) else data
        if not isinstance(listed, list):
            raise ValueError('not a list')
    except (OSError, ValueError):
        return _rec('job_scheduler', 'broken', _iso(now), target, 'the run records can be read, but the job list cannot', fix)
    return _rec('job_scheduler', 'available', _iso(now), target, f'{runs} runs on record and {len(listed)} jobs listed')


def names(life) -> list:
    out = list(BASE)
    try:
        import signedin as S
        out += [f'store:{site}' for site in S.sites(life)]
    except Exception:
        pass
    return out


def status(life, name: str, now=None, note=None) -> dict:
    """One capability. An unknown name is `unsupported`, never an error: a caller asking about something this engine
    has no check for gets a plain answer."""
    now = _dt(now)
    try:
        if name == 'inbox_feed':
            return inbox_feed(life, now, note)
        if name == 'calendar_copy':
            return calendar_copy(life, now, note)
        if name == 'page_reader':
            return page_reader(life, now)
        if name == 'own_chrome':
            return own_chrome(life, now)
        if name == 'texting_line':
            return texting_line(life, now)
        if name == 'job_scheduler':
            return job_scheduler(life, now)
        if str(name).startswith('store:'):
            return store(life, str(name)[6:], now)
    except Exception as e:
        return _rec(str(name), 'broken', None, str(name), f'its check itself broke ({type(e).__name__}: {str(e)[:80]})')
    return _rec(str(name), 'unsupported', None, str(name), 'there is no check for this')


def all(life, now=None, note=None) -> list:      # noqa: A001  (the name the spec gives it)
    now = _dt(now)
    return [status(life, n, now, note) for n in names(life)]


# ---- tick.py: the health note, and what this run could not read ----------------------------------------------------------

def health_part(life, note: dict, now=None) -> None:
    """tick.health_note's one call, after the other health parts. With the switch: `capabilities`, one entry per name
    (status, checked_at, evidence, recovery), judged from the note being written. Either of this module's switches that is
    present is listed under `switches`. With neither switch the note is exactly what it was. Never raises."""
    try:
        life = Path(life)
        here = [SWITCH, 'recover-on']   # 4-B: both folded in
        if here:
            note.setdefault('switches', {}).update({s: True for s in here})
        if on(life):
            note['capabilities'] = {r['name']: {k: r[k] for k in ('status', 'checked_at', 'evidence', 'recovery') if r.get(k)} for r in all(life, now, note)}
    except Exception as e:
        note['capability_health_error'] = f'{type(e).__name__}: {str(e)[:120]}'


def after_notice(life, report: dict, crashed: list, now=None) -> None:
    """tick.run_phases, right after the noticing pass. Writes down which source this pass could not read, and since when
    (the first pass that could not): the calendar copy, single emails that would not open, the pass itself breaking,
    and the mail feed as the last health note judged it. A source that reads again is taken off: after it is back, what
    it missed is read in the normal way, so nothing is owed about it. Nothing without the switch. Never raises."""
    if not on(life):
        return
    try:
        now = _dt(now)
        d = _load(life)
        was = d.get('unread') if isinstance(d.get('unread'), dict) else {}
        seen = {}
        meta = report.get('calendar') if isinstance(report, dict) else None
        if isinstance(meta, dict) and not meta.get('ok'):
            seen['calendar'] = {'what': 'calendar'}
        if any(str(c).startswith('notice:') for c in crashed or []):
            seen['noticing'] = {'what': 'noticing'}
        n = sum(1 for e in ((report or {}).get('errors') or []) if str(e).startswith('read email:'))
        if n:
            seen['email_reads'] = {'what': 'email_reads', 'n': n}
        box = (_health(life, now)[0] or {}).get('inbox_feed')
        if isinstance(box, dict) and box.get('ok') is False:
            seen['inbox_feed'] = {'what': 'inbox_feed', 'since': box.get('stale_since')}
        unread = {}
        for kind, s in seen.items():
            old = was.get(kind) if isinstance(was.get(kind), dict) else {}
            unread[kind] = {**old, **s, 'since': old.get('since') or s.get('since') or _iso(now), 'last': _iso(now)}
        if unread != was:
            d['unread'] = unread
            _save(life, d)
    except Exception as e:
        if isinstance(report, dict):
            report.setdefault('errors', []).append(f'capability: {type(e).__name__}: {str(e)[:120]}')


def unread_facts(life, now=None) -> list:
    """What could not be read, and for how long, as plain facts for the note writer: [{'kind', 'since', 'fact'}].
    One per source still unreadable, and only until a note has said it for this outage on this day. [] without the
    switch and when everything reads."""
    if not on(life):
        return []
    now = _dt(now)
    out = []
    today = now.astimezone(_tz()).date().isoformat()
    for kind, s in (_load(life).get('unread') or {}).items():
        if not isinstance(s, dict) or (s.get('said') or {}).get('day') == today:
            continue
        last = _moment(s.get('last'))
        if last is None or (now - last).total_seconds() > HEALTH_OLD:
            continue                                         # nothing has looked for a while: an old mark is not a fact about now
        since = _when(s.get('since'), now)
        fact = {'inbox_feed': f'My mail feed could not be read from {since} until now, so mail that came in during that time is unread by me. It is not "no new mail".',
                'calendar': f'Your calendar copy could not be read in full from {since} until now, so changes there during that time are unknown to me.',
                'noticing': f'My look for new mail and calendar changes broke before it finished, so what is new since {since} has not been looked at.',
                'email_reads': f"{int(s.get('n') or 1)} new email{'s' if int(s.get('n') or 1) != 1 else ''} could not be opened on my last look (first {since}), so I have not read {'them' if int(s.get('n') or 1) != 1 else 'it'}."}.get(kind)
        if fact:
            out.append({'kind': kind, 'since': s.get('since'), 'fact': fact})
    return out


def said_unread(text: str, kind: str) -> bool:
    """Does the note say that this source could not be read? The source's own word plus a "could not" of some kind."""
    low = str(text or '').lower()
    nouns = {'inbox_feed': ('mail', 'inbox', 'email'), 'email_reads': ('mail', 'email'), 'calendar': ('calendar',),
             'noticing': ('mail', 'calendar', 'inbox', 'email', 'check', 'look')}.get(kind, ())
    return any(n in low for n in nouns) and bool(_NOT_READ.search(low))


def composing(con, life, now_dt, compose, keep: list, out: dict):
    """notifier.deliver (through recover.composing): the writer for this note. With the switch, and only while a source
    is unreadable: the first item is handed the fact ("also_could_not_be_read"), with one short rule; in the morning
    note, or a note of three or more items, a writer that can take a check is also given it (the note must say the
    source could not be read). When the note that comes back says it, that is written down, so the same outage is said
    once a day. Nothing is appended to what the writer wrote. Otherwise: the writer it was given."""
    if not on(life):
        return compose
    try:
        now = _dt(now_dt)
        facts = unread_facts(life, now)
        if not facts:
            return compose
        import inspect
        try:
            takes = inspect.signature(compose).parameters
        except (TypeError, ValueError):
            takes = {}
        any_kw = any(p.kind == p.VAR_KEYWORD for p in takes.values())

        def wrapped(items, when, recent, **kw):
            shown, handed = [], False
            for it in items:
                if isinstance(it, dict) and not handed:
                    it, handed = {**it, 'also_could_not_be_read': [f['fact'] for f in facts]}, True
                shown.append(it)
            if not handed:
                return compose(items, when, recent, **kw)
            if 'rules' in takes or any_kw:
                kw['rules'] = str(kw.get('rules') or '') + UNREAD_RULE
            brief = len(items) >= 3 or 8 <= now.astimezone(_tz()).hour < 10
            if brief and ('must_say' in takes or any_kw):
                earlier = kw.get('must_say')
                kw['must_say'] = lambda text: (list(earlier(text)) if earlier else []) + [
                    {'quote': 'could not be read', 'why': UNREAD_WHY.format(fact=f['fact'])} for f in facts if not said_unread(text, f['kind'])]
            out['could_not_read'] = [f['kind'] for f in facts]
            result = compose(shown, when, recent, **kw)
            try:
                text = result.get('text') if isinstance(result, dict) else None
                said = [f['kind'] for f in facts if text and said_unread(text, f['kind'])]
                if said:
                    d = _load(life)
                    for kind in said:
                        if isinstance((d.get('unread') or {}).get(kind), dict):
                            d['unread'][kind]['said'] = {'day': now.astimezone(_tz()).date().isoformat(), 'at': _iso(now)}
                    _save(life, d)
                    out['could_not_read_said'] = said
            except Exception:
                pass
            return result
        try:
            wrapped.__signature__ = inspect.signature(compose)
        except (TypeError, ValueError):
            pass
        return wrapped
    except Exception as e:
        out.setdefault('capability_errors', []).append(f'{type(e).__name__}: {str(e)[:80]}')
        return compose


# ---- worker.py: what the job is told -------------------------------------------------------------------------------------

def needed_by(life, opp: dict) -> list:
    """The capabilities THIS job needs: each signed-in store its own words name, and with any store, Zeus's own Chrome."""
    try:
        import signedin as S
        stores = S.sites_needed(opp, S.sites(life))
    except Exception:
        stores = []
    return [f'store:{s}' for s in stores] + (['own_chrome'] if stores else [])


def prompt_lines(life, opp: dict, now=None) -> str:
    """worker.build_prompt (through recover.prompt_line): plain sentences about the capabilities this job needs that are
    NOT available, and nothing about any other. '' when all it needs is available or simply not proven either way
    (`stale` and `unsupported` say nothing about now, so the job is not told a guess), and '' without the switch.
    No field names, no status words, no ids."""
    if not on(life):
        return ''
    try:
        now = _dt(now)
        lines = []
        import signedin as S
        table = S.sites(life)
        for name in needed_by(life, opp):
            r = status(life, name, now)
            if name == 'own_chrome':
                if r['status'] == 'broken':
                    who = _chrome(life, now)[0]
                    lines.append('Your own browser is not answering right now, so anything that needs a signed-in site waits.' if who == 'unreachable' else
                                 'Your own browser could not be confirmed as yours right now, so do nothing in this run that needs a signed-in site.')
                    lines[-1] += " Never use the owner's browser instead. Finish everything else and say plainly what is waiting."
                continue
            store_name = str((table.get(name[6:]) or {}).get('name') or name[6:])
            if r['status'] == 'signed_out':
                lines.append(f"{store_name} is signed out in your own browser; {r['recovery'].split(';')[0]}. Do not try to sign in. "
                             f'Do everything that does not need {store_name} and say plainly what is waiting on the sign-in.')
            elif r['status'] == 'permission_required':
                lines.append(f"{store_name} is signed out in your own browser and only the owner can finish the sign-in; {r['recovery'].split(';')[-1].strip()}. "
                             f'Do not try to sign in. Do everything that does not need {store_name} and say plainly what is waiting on it.')
            elif r['status'] == 'broken':
                lines.append(f'Whether {store_name} is still signed in could not be read just now, so do not count on it: if a step there fails, say so plainly and stop that step.')
        return ('What you cannot use right now (checked by your own pipes):\n' + '\n'.join(f'- {x}' for x in lines) + '\n') if lines else ''
    except Exception:
        return ''
