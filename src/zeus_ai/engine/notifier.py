"""Notifier: one item per text, written by the real agent, after the work. No digests at any hour.

- Only items whose work is finished ("ready") are told, and each text carries ONE item: the most pressing one due now.
  The rest stay ready and each goes on a later run, if it still matters then (every gate below runs again).
- The moment it matters (`moment_gate`): a routine ("later") item whose own deadline or start is more than a day away
  is not told yet; its time moves to a day before (daytime). Silence stays the writer's call.
- Urgent never waits a run: after an urgent text, every other urgent item due goes in the same run, each on its own.
- Two-step gate: right before composing, re-check the source. If the owner already answered the email thread
  themselves, the item closes quietly and they hear nothing.
- A question the writer leaves out of the text is never recorded as asked; it goes again on the next run (once).
- The real agent writes the message (composer.py). It may decide nothing is worth a text, but it can never silence a
  real question.
- Quiet hours (settings.QUIET_START to settings.QUIET_END on the owner's clock, ownertime.alert_open): nothing goes
  then; held items are counted, never silent. The clock is read again right before sending, and once more right
  before the transport starts, so a note that took a minute to write cannot slip into quiet hours.
- Sends go through Hermes itself (HermesLine: `hermes --profile <profile> send`). The note and the items it carries
  are claimed in the ledger BEFORE the send, so a tick that dies mid-send is settled by reading the send back (the
  Hermes send journal), never by sending again.
- Without <LIFE_HOME>/notify-live nothing is sent and nothing is used up: items simply wait (counted). Sandboxes pass
  shadow_consume=True to log the note and move on.
- No daily message cap. RUNAWAY_TEXTS_PER_DAY is a loud circuit breaker far above a normal day; if it trips, items
  stay ready and the tick reports it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import composer as C  # noqa: E402
import prefs as F  # noqa: E402
import fresh as FR  # noqa: E402
import settings  # noqa: E402

from ownertime import PACIFIC, alert_open  # the zone the owner is in right now: the home zone unless they said otherwise (ownertime.py)
RUNAWAY_TEXTS_PER_DAY = 20   # circuit breaker, NOT a message cap: far above a normal day; tripping is reported, never silent
COMPOSE_FAILS_BEFORE_FALLBACK = 3
UNSETTLED_GIVE_UP = 30 * 60  # a note whose delivery cannot be confirmed either way after this long is assumed delivered, loudly
LEAD = 24 * 3600             # a routine item with its own deadline is told this long before it (moment_gate)
NOTE_ITEMS = 'CREATE TABLE IF NOT EXISTS note_items(note TEXT NOT NULL, opp TEXT NOT NULL, PRIMARY KEY(note, opp));'
_COMMON = {'your', 'this', 'that', 'with', 'from', 'have', 'will', 'send', 'reply', 'about', 'should', 'would', 'ready',
           'draft', 'check', 'plan', 'prepare', 'confirm', 'answer', 'today', 'tomorrow', 'before', 'after', 'whether', 'them', 'these'}
_COMMON |= {w for w in re.findall(r'[a-z0-9]{4,}', str(settings.OWNER_NAME).lower())}   # the owner's own name says nothing about the item


# ---- the agent's own texting line: Hermes itself ------------------------------------------------------------------

SEND_TIMEOUT = 90          # the whole send, process group included, is bounded at this
GRACE = 600                # a send is called "never left" only this long after it ended, on a clean journal read
ARRIVAL_WINDOW = 300       # an arrival this soon after an attempt can be that attempt
CHAT_QUIET = 600           # the owner texting the agent this close to it: the arrival may be a chat reply, not ours
POSITIVE = ('accepted', 'sent', 'delivered', 'read')
REFUSED = ('failed', 'cancelled')


def _norm(text):
    return ' '.join(str(text or '').split())


def _text_hash(text):
    return hashlib.sha256(_norm(text).encode()).hexdigest()[:24]


def run_bounded(argv, text, env, timeout, cwd):
    """One attempt with a deadline for the whole child process group."""
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(text, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.stdout.close()
            proc.stderr.close()
        raise


class Journal:
    """Hermes's send journal for the agent's profile (state.db, table outbound_attempts), read-only (mode=ro reads
    the WAL; immutable would skip it)."""
    def __init__(self, path=None, profile=None):
        self.path = Path(path or settings.STATE_DB)
        self.profile = profile or settings.PROFILE

    def rows(self, since, until=None):
        uri = self.path.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            sql = 'SELECT attempt_id, kind, content, state, created_at, updated_at, route_json FROM outbound_attempts ' \
                  'WHERE created_at >= ?' + (' AND created_at <= ?' if until is not None else '') + ' ORDER BY created_at'
            got = [dict(r) for r in db.execute(sql, (since,) if until is None else (since, until))]
        out = []
        for r in got:
            try:
                route = json.loads(r.pop('route_json') or '{}')
            except ValueError:
                route = {}
            if route.get('runtime_profile') not in (None, self.profile):
                continue
            r['created_at'] = float(r['created_at'])
            out.append(r)
        return out


class Arrivals:
    """Optional: when the owner's own Messages last got a text from the agent, and last sent one to it. Configured by
    settings `line.arrivals_url`, a local endpoint answering JSON {"reply": <ms epoch>, "me": <ms epoch>} (either may
    be null). Not configured or unreachable: raises, and every caller treats that as "cannot tell"."""
    def __init__(self, url=None, fetch=None, timeout=5):
        self.url = url if url is not None else settings.get('line.arrivals_url', '')
        self._fetch, self.timeout, self._got = fetch, timeout, None

    def _get(self):
        if not self.url:
            raise LookupError('no arrivals source configured')
        with urllib.request.urlopen(urllib.request.Request(self.url), timeout=self.timeout) as r:
            return json.loads(r.read().decode('utf-8'))

    def agent(self):
        """{'reply': secs | None (agent -> owner, last arrival), 'me': secs | None (owner -> agent)}."""
        if self._got is None:
            row = (self._fetch or self._get)()
            if not isinstance(row, dict):
                raise LookupError('arrivals source gave no answer')
            self._got = {k: (float(row[k]) / 1000.0 if isinstance(row.get(k), (int, float)) else None) for k in ('reply', 'me')}
        return self._got


class HermesLine:
    """The agent's own iMessage line: `hermes --profile <profile> send --to <platform>:<target> --json --file -`,
    bounded at SEND_TIMEOUT with the whole process group killed.

    send(text) -> {'kind': 'sent', 'id'} with the CLI's own success and message id, or the journal's provider acceptance
    of this exact text; {'kind': 'failed'} only when it provably never reached the provider (the CLI could not start,
    or the send journal shows no attempt at all in the send window); {'kind': 'failed', 'deferred': True} for quiet
    hours or a used-up tick budget (nothing was attempted); everything else {'kind': 'unknown'} (read back before any
    resend, never sent again blind).
    readback(text, since) -> True / False / None from the send journal; an attempt the journal left open is settled
    only by the optional arrivals source. False only after a clean read proves the text never left.
    destination(at) -> did a result delivered at `at` reach the owner's Messages? True / False / None.
    """
    def __init__(self, state_dir, clock=time.time, deadline=None, runner=None, journal=None, arrivals=None, dry=False,
                 capture=None, env_file=None):
        self.state = Path(state_dir)
        self.clock, self.deadline = clock, deadline
        self.runner = runner or run_bounded
        self.journal = journal or Journal()
        self.arrivals = arrivals or Arrivals()
        self.dry, self.capture = dry, (capture if capture is not None else [])
        self.env_file = Path(env_file or settings.PROFILE_DIR / '.env')
        self.log_path = self.state / 'line.sqlite3'

    # ---- our own log of every attempt (written before the CLI starts, so "no log row" = never started) ----------
    def _log(self):
        self.state.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.log_path), timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, h TEXT, started REAL, ended REAL, '
                   'outcome TEXT, rc INTEGER, message_id TEXT, error TEXT)')
        return db

    def _begin(self, text):
        with closing(self._log()) as db:
            cur = db.execute('INSERT INTO attempts(h, started) VALUES (?, ?)', (_text_hash(text), self.clock()))
            db.commit()
            return cur.lastrowid

    def _end(self, aid, outcome, rc=None, message_id=None, error=None):
        with closing(self._log()) as db:
            db.execute('UPDATE attempts SET ended=?, outcome=?, rc=?, message_id=?, error=? WHERE id=?',
                       (self.clock(), outcome, rc, message_id, (error or '')[:300] or None, aid))
            db.commit()

    def _attempts(self, text, since):
        if not self.log_path.exists():
            return None
        with closing(self._log()) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM attempts WHERE h=? AND started>=? ORDER BY started',
                                                (_text_hash(text), since))]

    def target(self):
        """The owner's chat handle: settings `line.target`, else PHOTON_HOME_CHANNEL in the profile's .env, else
        settings.OWNER_PHONE."""
        found = str(settings.get('line.target', '') or '').strip()
        if not found:
            try:
                for raw in self.env_file.read_text(errors='replace').splitlines():
                    line = raw.strip()
                    if line.startswith('export '):
                        line = line[7:].strip()
                    if line.startswith('PHOTON_HOME_CHANNEL='):
                        found = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                pass
        found = found or str(settings.OWNER_PHONE or '').strip()
        if re.fullmatch(r'\+\d{8,15}', found):
            return found
        raise RuntimeError("the owner's iMessage handle is not configured (settings owner.phone or line.target)")

    def argv(self, target):
        platform = str(settings.get('line.platform', 'photon'))
        return [settings.HERMES_BIN, '--profile', settings.PROFILE, 'send', '--to', platform + ':' + target, '--json', '--file', '-']

    def _closed(self, now):
        """Quiet hours on the home clock. ownertime.owners_clock_line moves this check to the owner's clock."""
        return not __import__('ownertime')._daytime_hour(datetime.fromtimestamp(now, ZoneInfo(settings.OWNER_TZ)).hour)

    # ---- send ---------------------------------------------------------------------------------------------------
    def send(self, text):
        now = self.clock()
        if self._closed(now):
            return {'kind': 'failed', 'deferred': True, 'error': 'quiet hours: nothing reaches the owner yet'}
        if self.dry:
            self.capture.append({'at': now, 'to': 'owner', 'line': "the agent's own iMessage line", 'text': text})
            return {'kind': 'sent', 'id': 'dry-%d' % len(self.capture)}
        if self.deadline is not None and self.deadline.expired():
            return {'kind': 'failed', 'deferred': True, 'error': 'tick time budget used up; next tick sends it'}
        try:
            target = self.target()
            aid = self._begin(text)
        except Exception as e:   # before any provider call: provably nothing went
            return {'kind': 'failed', 'error': '%s: %s' % (type(e).__name__, str(e)[:120])}
        started = self.clock()
        env = dict(os.environ, HERMES_PROFILE=settings.PROFILE, HERMES_DISABLE_LAZY_INSTALLS='1')
        cwd = str(settings.PROFILE_DIR) if settings.PROFILE_DIR.is_dir() else None
        try:
            rc, out, err = self.runner(self.argv(target), text, env, SEND_TIMEOUT, cwd)
        except subprocess.TimeoutExpired:
            self._end(aid, 'unknown', error='send timed out after %ds' % SEND_TIMEOUT)
            return {'kind': 'unknown', 'error': 'send timed out after %ds' % SEND_TIMEOUT}
        except OSError as e:   # the CLI never started: nothing went
            self._end(aid, 'failed', error='could not start the sender: ' + type(e).__name__)
            return {'kind': 'failed', 'error': 'could not start the sender: ' + type(e).__name__}
        except Exception as e:
            self._end(aid, 'unknown', error=type(e).__name__)
            return {'kind': 'unknown', 'error': type(e).__name__}
        try:
            value = json.loads(out) if (out or '').strip() else None
        except ValueError:
            value = None
        mid = value.get('message_id') if isinstance(value, dict) else None
        if rc == 0 and isinstance(value, dict) and value.get('success') and isinstance(mid, str) and mid.strip():
            self._end(aid, 'sent', rc=rc, message_id=mid.strip())
            return {'kind': 'sent', 'id': mid.strip()}
        why = (str(value.get('error')) if isinstance(value, dict) and value.get('error') else ' '.join(str(err or '').split()))
        kind = self._settle(text, started, self.clock())
        self._end(aid, kind, rc=rc, error='exit %s: %s' % (rc, why[:200]))
        return {'kind': kind, 'error': 'exit %s: %s' % (rc, why[:200] or 'no receipt')}

    def _settle(self, text, started, ended):
        """The CLI ended without its own receipt: what does the send journal say about this exact text?"""
        try:
            rows = self.journal.rows(started - 5, ended + 5)
        except Exception:
            return 'unknown'
        mine = [r for r in rows if _norm(r.get('content')) == _norm(text)]
        if mine:
            state = mine[-1]['state']
            return 'sent' if state in POSITIVE else ('failed' if state in REFUSED else 'unknown')
        return 'unknown' if rows else 'failed'   # no attempt admitted at all: it stopped before the provider call

    # ---- readback -----------------------------------------------------------------------------------------------
    def readback(self, text, since):
        now = self.clock()
        try:
            rows = self.journal.rows(since - 5)
        except Exception:
            return None
        mine = [r for r in rows if _norm(r.get('content')) == _norm(text)]
        if mine:
            r = mine[-1]
            if r['state'] in POSITIVE:
                return True
            if r['state'] in REFUSED:
                return False
            return self._arrived(r['created_at'], rows, exclude=r['attempt_id'])
        try:
            tries = self._attempts(text, since - 120)
        except Exception:
            return None
        if tries is None:
            return None          # no log at all: can't tell what happened
        if not tries:
            return False if now - since >= 60 else None   # this line never started a send for this text
        last = tries[-1]
        if last['ended'] is None:
            return None          # the host died mid-send; the CLI may still be running in its own session
        window = [r for r in rows if last['started'] - 5 <= r['created_at'] <= last['ended'] + 5]
        if window:
            return None          # something else was journaled then: can't attribute
        return False if now - last['ended'] >= GRACE else None

    def _arrived(self, at, rows, exclude=None):
        """True when the owner's Messages got a text from the agent right after `at` and nothing else went out then."""
        try:
            h = self.arrivals.agent()
        except Exception:
            return None
        reply, me = h.get('reply'), h.get('me')
        if reply is None or not (at - 2 <= reply <= at + ARRIVAL_WINDOW):
            return None
        if any(r['attempt_id'] != exclude and at - 2 <= r['created_at'] <= reply + 1 for r in rows):
            return None
        if me is not None and at - CHAT_QUIET <= me <= reply:
            return None
        return True

    def destination(self, delivered_at):
        """delivered_at: when the result went out (receipt / execution finish). True / False / None."""
        if delivered_at is None:
            return None
        now = self.clock()
        try:
            h = self.arrivals.agent()
        except Exception:
            return None
        reply, me = h.get('reply'), h.get('me')
        if reply is not None and delivered_at - 120 <= reply <= delivered_at + ARRIVAL_WINDOW:
            try:
                rows = self.journal.rows(delivered_at - 120, reply + 1)
            except Exception:
                return None
            if rows or (me is not None and delivered_at - CHAT_QUIET <= me <= reply):
                return None
            return True
        if (reply is None or reply < delivered_at - 120) and now - delivered_at >= GRACE:
            return False         # the owner's Messages got nothing from the agent since before this result went out
        return None


def _line(state_dir):
    """The texting line every engine send uses, with its quiet-hours check on the owner's clock."""
    OT = __import__('ownertime')
    return OT.owners_clock_line(HermesLine)(state_dir)


def _default_recheck(row, opp):
    """True = the owner already answered this email thread themselves since it was noticed. None = cannot tell."""
    mail = __import__('integrity').own_mail(opp, opp.get('latest_email') or {})   # commit integrity: a newest-email mark that points at another thread than the item's own is not used. The same mark without the switch
    mailbox, message_id, thread_id = mail.get('mailbox'), mail.get('message_id'), mail.get('thread_id')
    if not message_id:
        try:
            ids = [s for s in json.loads(row['sources'] or '[]') if str(s).startswith('email:')]
        except ValueError:
            ids = []
        if len(ids) != 1:
            return None      # several mails merged and we do not know the newest: cannot tell, so do not drop it
        _, mailbox, message_id = ids[0].split(':', 2)
        thread_id = (opp.get('thread') or {}).get('thread_id')
    try:
        import sources as S
        # sources.py's "did the owner answer this thread since?" check, found by its name's ending
        check = getattr(S, 'owner_replied_since', None) or next(
            (getattr(S, n) for n in dir(S) if n.endswith('_replied_since') and callable(getattr(S, n))), None)
        return check(mailbox, message_id, thread_id) if check else None
    except Exception:
        return None


def _fails(life_home: Path, bump=None) -> int:
    f = Path(life_home) / 'compose-fails'
    try:
        n = int(f.read_text().strip() or 0)
    except (OSError, ValueError):
        n = 0
    if bump is True:
        n += 1
        f.write_text(str(n))
    elif bump is False and n:
        f.unlink(missing_ok=True)
        n = 0
    return n


def next_daytime(now_dt: datetime) -> datetime:
    """The next moment a routine nudge may go: now outside quiet hours, else the next morning (ownertime.alert_open)."""
    return alert_open(now_dt)


def _when(opp):
    for k in ('deadline', 'event_start'):
        try:
            t = datetime.fromisoformat(str(opp.get(k)).replace('Z', '+00:00'))
            return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).timestamp()
        except (TypeError, ValueError):
            continue
    return float('inf')


def _daytime(t: datetime) -> datetime:
    """t held inside daytime hours on the same day (earlier rather than later, so it stays ahead of what it is about)."""
    start, end = settings.QUIET_END, settings.QUIET_START
    if start < end and t.hour < start:
        return t.replace(hour=start, minute=0, second=0, microsecond=0)
    if start < end and t.hour >= end:
        return t.replace(hour=max(start, end - 1), minute=0, second=0, microsecond=0)
    return t


def moment_gate(con, life_home, keep: list, now_dt: datetime, out: dict) -> list:
    """No digest: each ready routine item is told at the moment it matters.
    A "later" item whose own deadline or start is more than LEAD away waits: its time becomes LEAD before that,
    in daytime, and then it goes. An item the owner may not be involved in (owner_involved unknown) counts as "later".
    Whether a finished item is worth a text at all stays with the note writer: code never forces silence.
    -> the items still due now."""
    go = []
    for r, opp in keep:
        if r['urgency'] != 'later' and opp.get('owner_involved') != 'unknown':   # not sure it is the owner's: treated as routine
            go.append((r, opp))
            continue
        when = _when(opp)
        if when != float('inf') and when - now_dt.timestamp() > LEAD:
            at = _daytime(datetime.fromtimestamp(when - LEAD, now_dt.tzinfo))
            opp['timed_for_its_moment'] = {'from': r['surface_at'], 'to': at.isoformat(), 'at': time.time()}
            con.execute("UPDATE opps SET surface_at=?, payload=?, updated=? WHERE key=? AND state='ready'", (at.isoformat(), json.dumps(opp, default=str), time.time(), r['key']))
            out['timed_for_its_moment'] = out.get('timed_for_its_moment', 0) + 1
            continue
        go.append((r, opp))
    con.commit()
    return go


def most_pressing(keep: list, con=None, now_dt=None) -> list:
    """The one item this text carries: urgent first, then "today"; inside that, the one whose texts failed least often
    (so one item the line keeps refusing never blocks the rest), then the pressing ones (due within a day, a person
    waiting on the owner's reply, already waited once), then the soonest deadline, then the
    oldest. The rest stay ready for a later run."""
    rank = {'now': 0, 'today': 1, 'later': 2}
    soon = (now_dt.timestamp() if now_dt else time.time()) + 86400

    def pressing(r, o):
        steps = (o.get('decision') or {}).get('steps') or []
        return (_when(o) <= soon or bool(o.get('asked_later'))
                or (o.get('_source') == 'email' and any(s.get('action_class') == 'message_outside_person' for s in steps)))

    def failed(key):
        if con is None:
            return 0
        return con.execute("SELECT COUNT(*) FROM note_items n JOIN outbox o ON o.key=n.note WHERE n.opp=? AND o.state='failed'", (key,)).fetchone()[0]
    return sorted(keep, key=lambda p: (rank.get(p[0]['urgency'], 3), failed(p[0]['key']), not pressing(*p), _when(p[1]), p[0]['first_seen'] or 0))[:1]


def mentioned(text: str, opp: dict) -> bool:
    """Does the note actually put this item's question to the owner? A rough word check, only used for items with a decision."""
    ask = ((opp.get('packet') or {}).get('decision') or {}).get('ask') or ''
    words = {w for w in re.findall(r'[a-z0-9]{4,}', f"{opp.get('title') or ''} {ask}".lower())} - _COMMON
    if not words:
        return True
    low = text.lower()
    hits = sum(1 for w in words if w in low)
    return hits >= 2 or (hits >= 1 and len(words) <= 3)


def recent_texts(con, now_ts: float, include_logged: bool = False) -> list:
    states = ('sent', 'unknown', 'sent_unconfirmed') + (('logged',) if include_logged else ())
    rows = con.execute(f"SELECT text FROM outbox WHERE at>=? AND state IN ({','.join('?' * len(states))}) ORDER BY at",
                       (now_ts - 86400, *states)).fetchall()
    return [r['text'] for r in rows if r['text']]


def close_children(con, parent_key: str, why: str):
    """Follow-up checks hanging off an item that just closed stop too."""
    for c in con.execute("SELECT key,payload FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready')", (parent_key + '#fu%',)).fetchall():
        p = json.loads(c['payload'])
        p['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'engine', 'why': why}
        con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=?", (json.dumps(p, default=str), time.time(), c['key']))


def _mark_told(con, key: str, note_key: str, uncertain: bool = False) -> bool:
    """Mark one item told, only if it is still ready: a drop from the owner's chat while the note was being written wins."""
    row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
    if not row or row['state'] != 'ready':
        return False
    p = json.loads(row['payload'])
    p['told'] = {'at': time.time(), 'note': note_key} | ({'uncertain': True} if uncertain else {})
    state = 'surfaced' if (p.get('packet') or {}).get('decision') else 'done'
    state = __import__('integrity').told_state(con, p, note_key, state)   # commit integrity: a note that provably went is the item's receipt; one whose delivery is not known leaves the item open. The same state without the switch
    con.execute("UPDATE opps SET state=?, payload=?, updated=? WHERE key=? AND state='ready'", (state, json.dumps(p, default=str), time.time(), key))
    return True


def _reopen(con, note_key: str) -> int:
    n = 0
    for o in con.execute("SELECT key,payload FROM opps WHERE state IN ('surfaced','done') AND json_extract(payload,'$.told.note')=?", (note_key,)).fetchall():
        p = json.loads(o['payload'])
        p.pop('told', None)
        p['resend'] = {'after_failed_note': note_key, 'at': time.time()}
        con.execute("UPDATE opps SET state='ready', payload=?, updated=? WHERE key=?", (json.dumps(p, default=str), time.time(), o['key']))
        n += 1
    return n


def _settle(con, line, life_home: Path, out: dict, now_ts: float, give_up: bool = True) -> set:
    """Earlier notes whose delivery is not known yet: 'sending' (a tick died mid-send) or 'unknown' (no receipt).
    His own Messages are read back; nothing is ever sent again blind. Returns item keys that must wait this tick.
    give_up=False (the `off` switch): read back only. A note that still cannot be told either way stays uncertain;
    it is not written off as delivered and no ticket is raised until the pipes run again."""
    waiting = set()
    for r in con.execute("SELECT key,text,at,state FROM outbox WHERE mode='live' AND state IN ('sending','unknown')").fetchall():
        items = [x['opp'] for x in con.execute('SELECT opp FROM note_items WHERE note=?', (r['key'],)).fetchall()]
        ok = line.readback(r['text'], r['at'] - 5)
        if ok is None and give_up and now_ts - r['at'] > UNSETTLED_GIVE_UP:
            __import__('urgentfallback').text_failed(con, life_home, r['key'], items, now_ts, 'unconfirmed', ticket_exists=True)   # switch urgent-fallback-on: an urgent item of a note that cannot be confirmed gets ONE email; the note's own ticket is the one raised below. Nothing without the switch
        if ok is True:
            con.execute("UPDATE outbox SET state='sent' WHERE key=?", (r['key'],))
            for k in items:
                _mark_told(con, k, r['key'])
            __import__('integrity').confirmed(con, r['key'])   # commit integrity: items left open by an uncertain send get their receipt now. Nothing without the switch
        elif ok is False:                       # it provably never reached the owner: those items go again
            con.execute("UPDATE outbox SET state='failed' WHERE key=?", (r['key'],))
            out['reopened'] = out.get('reopened', 0) + _reopen(con, r['key'])
            if give_up:
                __import__('urgentfallback').text_failed(con, life_home, r['key'], items, now_ts, 'never_arrived')   # switch urgent-fallback-on: one ticket now; its one email goes on the next run only if the text that goes again below did not reach the owner either. Nothing without the switch
        elif give_up and now_ts - r['at'] > UNSETTLED_GIVE_UP and not __import__('integrity').never_assumed(con, life_home, r['key']):   # commit integrity: with the switch it is never written off as delivered (read back every run, one ticket)
            con.execute("UPDATE outbox SET state='sent_unconfirmed' WHERE key=?", (r['key'],))
            for k in items:
                _mark_told(con, k, r['key'], uncertain=True)
            out['unconfirmed_notes'] = out.get('unconfirmed_notes', 0) + 1
            try:
                import worker as W
                W._alarm(life_home, r['key'], f"{settings.AGENT_NAME}'s note to the owner: delivery could not be confirmed after 30 minutes (it was not sent again)", kind='life_note_unconfirmed')
            except Exception:
                pass
        elif r['state'] == 'sending':
            waiting.update(items)               # do not put these in another note until we know
    con.commit()
    return waiting


def settle_only(con, now_dt: datetime, life_home: Path, line=None) -> dict:
    """All a tick does about notes while the `off` switch is present: find out, by reading back, what happened to a
    note that was already on its way. Nothing is written, composed or sent, and no ticket is raised. Proved delivered:
    its items are marked told. Proved never sent: its items go back to waiting (they are sent only after resume).
    Cannot tell: left as it is for the next run. -> counts for the health note.
    `life stop` also takes every off-machine deadline backup draft down and reads the scheduled list back
    (urgentfallback.deadlines, switch urgent-fallback-on); stop = silence. Nothing is sent."""
    def unsettled():
        return con.execute("SELECT COUNT(*) FROM outbox WHERE mode='live' AND state IN ('sending','unknown')").fetchone()[0]

    backup = __import__('urgentfallback').deadlines(con, life_home, now_dt)   # None without the switch or with nothing to take down
    con.executescript(NOTE_ITEMS)
    before = unsettled()
    if not before:
        return {'uncertain_notes': 0} | ({'deadline_backup': backup} if backup else {})
    life_home = Path(life_home)
    if line is None:
        (life_home / 'line').mkdir(parents=True, exist_ok=True)
        line = _line(life_home / 'line')
    out = {}
    _settle(con, line, life_home, out, now_dt.timestamp(), give_up=False)
    left = unsettled()
    return {'uncertain_notes': before, 'settled': before - left, 'still_uncertain': left, 'reopened_items': out.get('reopened', 0)} | ({'deadline_backup': backup} if backup else {})


def _send(con, line, key: str, text: str, item_keys: list, *, clock=None, life=None) -> str:
    """-> 'sent' | 'unknown' | 'deferred' | 'failed' | 'dup'. The note and its items are claimed before the send."""
    if con.execute('SELECT 1 FROM outbox WHERE key=?', (key,)).fetchone():
        return 'dup'
    if any('#fu-peer:' in k and not __import__('onesend').peer_report_current(con, k) for k in item_keys):
        return 'deferred'
    if not __import__('integrity').may_send(con, item_keys, text=text):   # commit integrity (switch commit-integrity-on): the owner's stop, each item's state and version, and the send permission are read again right here; one that fails holds this note, nothing is claimed or sent. True without the switch
        return 'deferred'
    con.execute('INSERT INTO outbox VALUES(?,?,?,?,?)', (key, text, 'live', time.time(), 'sending'))
    con.executemany('INSERT OR IGNORE INTO note_items VALUES(?,?)', [(key, k) for k in item_keys])
    con.commit()
    at_send = (clock or (lambda: datetime.now(PACIFIC)))()
    if alert_open(at_send, life=life) > at_send:
        # This claim has not reached the transport. Use the existing deferred path to release it.
        res = {'kind': 'failed', 'deferred': True}
    else:
        res = line.send(text)
    kind = res.get('kind')
    if kind == 'sent' and (res.get('message_id') or res.get('id')):
        for child in item_keys:
            if '#fu-peer:' in child:
                con.execute("UPDATE opps SET payload=json_set(payload,'$.owner_message_id',?) WHERE key=?",
                            (str(res.get('message_id') or res['id']),child))
    if kind == 'failed' and res.get('deferred'):
        con.execute('DELETE FROM outbox WHERE key=?', (key,))   # provably not sent (the line's own hours): try later
        con.execute('DELETE FROM note_items WHERE note=?', (key,))
        con.commit()
        return 'deferred'
    kind = kind if kind in ('sent', 'unknown') else 'failed'
    con.execute('UPDATE outbox SET state=? WHERE key=?', (kind, key))
    con.commit()
    return kind


def _quiet_hold(con, rows, now_dt, life, out):
    morning = alert_open(now_dt, life=life)
    if morning <= now_dt:
        return False
    for row in rows:
        hold_for_morning(con, row, now_dt, morning)
    out['held'] += len(rows)
    out['before_nine' if now_dt.astimezone(morning.tzinfo).hour < 9 else 'night_stop'] = True
    return True


def hold_for_morning(con, row, now_dt: datetime, morning: datetime):
    """Keep the work and its original time for the next morning's source check."""
    why = 'waiting for the morning on the owner clock'
    opp = json.loads(row['payload'])
    opp['held_overnight'] = {'at': now_dt.isoformat(), 'until': morning.isoformat(), 'why': str(why)[:200], 'was_due': row['surface_at']}
    if isinstance(opp.get('decision'), dict):
        opp['decision']['surface_at'] = morning.isoformat()
    con.execute("UPDATE opps SET surface_at=?, payload=?, updated=? WHERE key=? AND state='ready'",
                (morning.isoformat(), json.dumps(opp, default=str), time.time(), row['key']))
    con.commit()


def bare_reminder(opp: dict) -> str:
    """'' or why this ready item is not a text: a missed ask or an open loop with nothing prepared for the owner's
    yes, or a follow-up check that did no work. There is nothing to say yes to and nothing new to tell, so such a
    text would only repeat code's own notes. A reminder the OWNER asked for and a code-worded moment are never one
    of these."""
    p = opp.get('packet') if isinstance(opp.get('packet'), dict) else {}
    fu = opp.get('followup') if isinstance(opp.get('followup'), dict) else {}
    if fu.get('reminder') or p.get('verbatim'):
        return ''
    if isinstance(opp.get('miss'), dict) and opp['miss'].get('form') == 'tell':
        return 'a missed ask with nothing done and nothing prepared'
    ready = str((p.get('decision') or {}).get('ready') or '').strip() if isinstance(p.get('decision'), dict) else ''
    asked = str((p.get('decision') or {}).get('ask') or '').strip() if isinstance(p.get('decision'), dict) else ''
    if opp.get('_source') == 'loops' and not ready and (p.get('prep') or not asked):
        return 'an open loop with nothing prepared for the owner\'s yes'      # a job that came back with a real question (no "prep", an ask) is not this
    if fu and p.get('prep'):
        return 'a follow-up check that did no work'
    return ''


def _newest_moment_per_watch(con, rows: list, life_home=None) -> list:
    """deliver_verbatim: moments that waited (a night, a deferred send). Per watch only
    the newest is told, since it says where things stand. One the watch has since moved past (code already told a later
    change) is not sent at all. A correction with its own landing still unsent beside it corrects nothing: both go."""
    def num(key) -> int:
        tail = str(key).rsplit(':', 1)[-1]
        return int(tail) if tail.isdigit() else 0
    told_n = {}
    try:
        d = __import__('watches').Store(life_home).load() or {} if life_home is not None else {}
        told_n = {w.get('id'): int(((w.get('monitor') or {}).get('last_code_told') or {}).get('n') or 0) for w in d.get('watches') or []}
    except Exception:
        told_n = {}
    opps = {r['key']: json.loads(r['payload']) for r in rows}
    by_watch = {}
    for r in rows:
        o = opps[r['key']]
        if o.get('moment') and o.get('monitor_watch'):
            by_watch.setdefault(o['monitor_watch'], []).append(r['key'])
    drop = {}
    for w, keys in by_watch.items():
        keys.sort(key=num)
        newest = keys[-1]
        for k in keys[:-1]:
            drop[k] = 'a later moment of the same watch says where things stand'
        if opps[newest].get('moment') == 'correction' and any(opps[k].get('moment') in ('landed', 'arrived') for k in keys[:-1]):
            drop[newest] = 'the landing it corrects never reached the owner'
        elif told_n.get(w, 0) > num(newest):
            drop[newest] = 'the watch has told the owner later news since'
    keep = []
    for r in rows:
        if r['key'] in drop:
            o = opps[r['key']]
            o['closed'] = {'state': 'done', 'at': time.time(), 'by': 'notifier', 'why': drop[r['key']]}
            con.execute("UPDATE opps SET state='done', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(o, default=str), time.time(), r['key']))
            continue
        keep.append(r)
    return keep


def _morning_texts(con, life, rows, now_dt):
    """Coalesce delayed flight updates before a send. Preserve each independent daytime notice."""
    import monitor
    groups = {}
    entries = []
    for row in rows:
        item = json.loads(row['payload'])
        trip_id = item.get('flight_trip')
        if trip_id:
            groups.setdefault(trip_id, []).append((row, item))
        else:
            entries.append((False, row, monitor.held_text(life, item, now_dt)))
    def close(row):
        con.execute("""UPDATE opps SET state='done', updated=?, payload=json_set(payload,'$.closed',json(?))
            WHERE key=? AND state='ready'""",
            (time.time(), json.dumps({'by':'notifier','state':'done','why':'superseded at the morning flight recheck'}), row['key']))

    def overnight(pair):
        row, item = pair
        if item.get('held_overnight'):
            return True
        observed = datetime.fromtimestamp(row['first_seen'], now_dt.tzinfo)
        return observed < now_dt.replace(hour=9,minute=0,second=0,microsecond=0)

    for trip_id, group in groups.items():
        group.sort(key=lambda pair: int(pair[0]['key'].rsplit(':', 1)[-1]))
        if not any(overnight(pair) for pair in group):
            entries.extend((item.get('moment') == 'cancelled', row, item['packet']['headline']) for row,item in group)
            continue
        trip = next((t for t in monitor.trips(life) if t['id'] == trip_id), None)
        if not trip or trip.get('state') == 'stopped' or not trip.get('legs'):
            for row,_ in group:
                close(row)
            continue
        source = monitor.FlightSource(life)     # the same source, environment and monthly cap as the watch itself
        fresh = {}
        def read(leg):
            key = (leg['ident'], leg['date'])
            if key not in fresh:
                fresh[key] = source.facts(leg, now_dt)
            return fresh[key]

        def stage(row, kind, words, terminal):
            if terminal:
                prior = con.execute("""SELECT 1 FROM opps o JOIN note_items n ON n.opp=o.key JOIN outbox b ON b.key=n.note
                    WHERE json_extract(o.payload,'$.flight_trip')=? AND (json_extract(o.payload,'$.fingerprint')=? OR
                    json_extract(o.payload,'$.morning_final')=?) AND b.mode='live'
                    AND b.state IN ('sent','sending','unknown','sent_unconfirmed')""", (trip_id,terminal,terminal)).fetchone()
                if prior:
                    close(row)
                    return
                con.execute("UPDATE opps SET payload=json_set(payload,'$.morning_final',?) WHERE key=?", (terminal,row['key']))
            entries.append((kind=='cancelled',row,words))

        def unread(*lists):
            """The source gave no current reading. Rather than wait, unsaid, for as long as the source stays down, the
            tries are counted on the newest row held back; at monitor.FAILS_BEFORE_NOTE the owner gets the newest
            fact of each list once, dated and marked unconfirmed, and the older rows close."""
            row, item = lists[0][-1]
            tries = int(item.get('morning_tries') or 0) + 1
            con.execute("UPDATE opps SET payload=json_set(payload,'$.morning_tries',?) WHERE key=?", (tries, row['key']))
            if tries < monitor.FAILS_BEFORE_NOTE:
                return
            for n, pairs in enumerate(filter(None, lists)):
                for older,_ in pairs[:-1]:
                    close(older)
                row, item = pairs[-1]
                seen = datetime.fromtimestamp(row['first_seen'], now_dt.tzinfo)
                when = seen.strftime('%I:%M%p').lstrip('0').lower() + (' last night' if seen.date() < now_dt.date() else '')
                entries.append((False, row, f"Also unconfirmed, from {when}: {item['packet']['headline']}" if n else
                                f"I could not get a confirmed reading from the flight source this morning. The last I had, from {when}: {item['packet']['headline']} Treat that as unconfirmed, not current."))

        status = [(r,o) for r,o in group if o.get('moment') not in monitor.PICKUP_MOMENTS]
        pickups = [(r,o) for r,o in group if o.get('moment') in monitor.PICKUP_MOMENTS]
        final_leg = trip['legs'][-1]
        final_tag = final_leg['ident'].lower() + '-' + final_leg['date'] + '-'
        if status:
            kind, words, terminal, owner = monitor.morning_flight(trip, [o for _,o in status], now_dt, read)
            if kind == 'retry':
                unread(status, pickups)
                continue
            selected = next(r for r,o in status if o is owner)
            for row,_ in status:
                if row['key'] != selected['key']:
                    close(row)
            stage(selected, kind, words, terminal)
            if kind in ('cancelled','diverted') or (kind == 'landed' and terminal.startswith(final_tag)):
                for row,_ in pickups:
                    close(row)
                continue
        if pickups:
            newest = pickups[-1]
            for row,_ in pickups[:-1]:
                close(row)
            row, item = newest
            if not overnight(newest):
                entries.append((False, row, item['packet']['headline']))
                continue
            kind, words, terminal, advice = monitor.morning_pickup(life, trip, item, now_dt, read)
            if kind == 'retry':
                unread([newest])
                continue
            if kind == 'drop':
                close(row)
                continue
            if advice.get('leave_by'):
                # The normal pickup's receipt reconciliation can now see precisely the recommendation sent in the morning.
                con.execute("UPDATE opps SET payload=json_set(payload,'$.moment',?,'$.leave_by',?) WHERE key=?",
                            (advice['kind'],advice['leave_by'],row['key']))
            stage(row, kind, words, terminal)
    entries.sort(key=lambda entry: not entry[0])
    return [row for _,row,_ in entries], '\n\n'.join(text for _,_,text in entries)


def deliver_verbatim(con, now_dt: datetime, life_home: Path, *, live: bool, line=None, clock=None, night=None) -> dict:
    """monitor.py's code-worded texts (packet 'verbatim'), word for word, on their own: no note writer, no model, not
    folded into another note. The hours rule, receipt and morning source check apply to every item."""
    out = {'sent': 0, 'unknown': 0, 'held': 0, 'told': 0}
    if not (Path(life_home) / 'watches-on').exists():
        return out
    con.executescript(NOTE_ITEMS)
    rows = [r for r in con.execute("SELECT * FROM opps WHERE state='ready' AND julianday(surface_at)<=julianday(?) ORDER BY surface_at", (now_dt.isoformat(),)).fetchall()
            if (json.loads(r['payload']).get('packet') or {}).get('verbatim')]
    if not rows:
        if live:
            _quiet_hold(con, rows, now_dt, life_home, out)
        return out
    if not live:
        out['held'] = len(rows)
        out['not_live'] = True
        return out
    if _quiet_hold(con, rows, now_dt, life_home, out):
        return out
    day0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    sent_today = con.execute("SELECT COUNT(*) FROM outbox WHERE mode='live' AND state IN ('sent','unknown','sending','sent_unconfirmed') AND at>=?", (day0,)).fetchone()[0]
    if sent_today >= RUNAWAY_TEXTS_PER_DAY:
        out['held'] += len(rows)
        out['runaway'] = True
        return out
    line = line or _line(Path(life_home) / 'line')
    tests = [r for r in rows if json.loads(r['payload']).get('release_test')]
    if tests:                                    # a release test goes alone and word for word, never joined with anything
        rows = tests[:1]
    current = []
    for row in rows:
        if '#fu-peer:' in row['key'] and not __import__('onesend').peer_report_current(con, row['key']):
            con.execute("UPDATE opps SET state='moot' WHERE key=? AND state='ready'", (row['key'],))
        else:
            current.append(row)
    con.commit()
    rows = _newest_moment_per_watch(con, current, life_home)
    if not rows:
        con.commit()
        return out
    rows, text = _morning_texts(con, Path(life_home), rows, now_dt)
    con.commit()
    if not rows:
        return out
    if (Path(life_home) / 'timer-truth-on').exists():   # timers: a change found by a read that ran late says so, in code's own words; the same text otherwise
        text = __import__('timers').verbatim_late(con, life_home, rows, text, now_dt)
    key = 'note:' + now_dt.strftime('%Y-%m-%dT%H:%M') + (':test' if tests else ':code')
    kind = _send(con, line, key, text, [r['key'] for r in rows], clock=clock, life=life_home)
    if kind in ('deferred', 'dup', 'failed'):
        out['held'] += len(rows)
        out['send'] = kind
        if kind == 'failed':
            __import__('urgentfallback').text_failed(con, life_home, key, [r['key'] for r in rows], now_dt)   # switch urgent-fallback-on: an urgent text that failed gets one ticket and ONE email. Nothing without the switch
        return out
    out['sent'] += kind == 'sent'
    out['unknown'] += kind == 'unknown'
    out['told'] = sum(1 for r in rows if _mark_told(con, r['key'], key))
    con.commit()
    out['note'], out['text'] = key, text
    return out


def deliver(con, now_dt: datetime, life_home: Path, *, live: bool, compose=None, recheck=None, line=None,
            shadow_consume: bool = False, clock=None, night=None) -> dict:
    """One routine item per run. Urgent never waits a run: after an urgent text, every other urgent item
    still due goes now, each as its own text, through the same gates (quiet hours, night readings, breaker, claim)."""
    kw = dict(live=live, compose=compose, recheck=recheck, line=line, shadow_consume=shadow_consume, clock=clock, night=night)
    out = _deliver_once(con, now_dt, life_home, **kw)
    last, n = out, 0
    while last.get('told_urgent') and n < RUNAWAY_TEXTS_PER_DAY:
        n += 1
        last = _deliver_once(con, now_dt, life_home, urgent_only=n, **kw)
        for k, v in last.items():
            if isinstance(v, int) and not isinstance(v, bool) and isinstance(out.get(k, 0), int):
                out[k] = out.get(k, 0) + v
            elif k == 'note':
                out.setdefault('more_notes', []).append(v)
            else:
                out.setdefault(k, v)
    return out


def _deliver_once(con, now_dt: datetime, life_home: Path, *, live: bool, compose=None, recheck=None, line=None,
                  shadow_consume: bool = False, clock=None, night=None, urgent_only: int = 0) -> dict:
    out = {'sent': 0, 'unknown': 0, 'held': 0, 'told': 0, 'settling': 0, 'silent': 0, 'moot': 0}
    __import__('urgentfallback').CLOCK = clock or (lambda: datetime.now(PACIFIC))   # switch urgent-fallback-on: the stand-in email reads the same real clock this note reads right before a send. Nothing is read without the switch
    if not urgent_only and (Path(life_home) / 'watches-on').exists():     # monitor.py: code-worded texts go first, on their own
        out['code_texts'] = deliver_verbatim(con, now_dt, life_home, live=live, line=line, clock=clock, night=night)
    life_home = Path(life_home)
    compose = compose or C.compose
    recheck = recheck or _default_recheck
    clock = clock or (lambda: datetime.now(PACIFIC))
    now_ts = now_dt.timestamp()
    con.executescript(NOTE_ITEMS)
    waiting = set()
    if live and urgent_only:                     # another urgent text in this same run: the run's checks above already ran
        line = line or _line(life_home / 'line')
    elif live:
        (life_home / 'line').mkdir(parents=True, exist_ok=True)
        line = line or _line(life_home / 'line')
        waiting = _settle(con, line, life_home, out, now_ts)
        fell = __import__('urgentfallback').tend(con, life_home, now_dt)   # switch urgent-fallback-on: an email that waited for the morning goes if still needed, an unknown one is settled by reading the owner's inbox. None without the switch
        if fell:
            out['urgent_fallback'] = fell
        judged = __import__('outcome').judge_ready(con, life_home, now_dt)   # switch own-outcome-on: a finished result about to be told is put to the close judge first; its kept answer is what lets the item close. None without the switch
        if judged:
            out['outcome_judge'] = judged
    ready = [r for r in con.execute("SELECT * FROM opps WHERE state='ready' AND julianday(surface_at)<=julianday(?) ORDER BY urgency='now' DESC, surface_at",
                                    (now_dt.isoformat(),)).fetchall() if r['key'] not in waiting and (not urgent_only or r['urgency'] == 'now')]
    if waiting:
        out['unsettled'] = len(waiting)
    ready = __import__('research').old_lane(life_home, ready)   # research.py (switch research-flow-on): this lane keeps what the owner asked for themselves and code texts; the rest waits as a pending update for the hourly research. The same list without the switch
    if not ready:
        return out
    if not live and not shadow_consume:
        out['held'] = len(ready)                 # sending is switched off: nothing is used up, items wait
        out['not_live'] = True
        return out
    if _quiet_hold(con, ready, now_dt, life_home, out):
        return out
    batch = ready
    day0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    sent_today = con.execute("SELECT COUNT(*) FROM outbox WHERE mode='live' AND state IN ('sent','unknown','sending','sent_unconfirmed') AND at>=?", (day0,)).fetchone()[0]
    if live and sent_today >= RUNAWAY_TEXTS_PER_DAY:
        out['held'] += len(batch)
        out['runaway'] = True                    # breaker tripped: items stay ready, the tick reports it
        return out

    keep = []
    for r in batch:
        opp = json.loads(r['payload'])
        if F.is_muted(life_home, r['key']):      # the owner said to stop raising this kind of thing
            opp['closed'] = {'state': 'declined', 'at': time.time(), 'by': 'notifier', 'why': 'muted kind'}
            con.execute("UPDATE opps SET state='declined', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), time.time(), r['key']))
            close_children(con, r['key'], 'parent muted')
            continue
        # two-step gate: is it still needed, right now? (a follow-up check is not gated: the owner's own reply is expected there)
        if not opp.get('followup') and '#fu' not in r['key'] and recheck(r, opp) is True:
            opp['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'notifier', 'why': 'the owner already answered the thread themselves'}
            con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), time.time(), r['key']))
            close_children(con, r['key'], 'parent closed: the owner already answered')
            out['moot'] += 1
            continue
        told_by_code = __import__('monitor').already_told_by_code(con, r) if (life_home / 'watches-on').exists() else ''
        if told_by_code:                         # monitor.py: code already texted the owner this change; never said twice
            opp['closed'] = {'state': 'done', 'at': time.time(), 'by': 'notifier', 'why': f'code already told the owner ({told_by_code})'}
            con.execute("UPDATE opps SET state='done', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), time.time(), r['key']))
            out['already_told_by_code'] = out.get('already_told_by_code', 0) + 1
            continue
        bare = bare_reminder(opp)
        if bare:                                 # closed quietly; the loop or the review still holds it
            opp['closed'] = {'state': 'done', 'at': time.time(), 'by': 'notifier', 'why': 'not a text: ' + bare}
            opp['told'] = {'at': time.time(), 'silent': True}
            con.execute("UPDATE opps SET state='done', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), time.time(), r['key']))
            out['bare_reminders'] = out.get('bare_reminders', 0) + 1
            continue
        keep.append((r, opp))
    con.commit()
    keep = moment_gate(con, life_home, keep, now_dt, out)   # the moment it matters, or silence; never a digest
    if not keep:
        return out

    # Nothing stale (fresh.py, switch fresh-on): every item is read again and compared with what its result was built
    # from. Changed: back for fresh work. Unreadable: it waits, then goes and says so. Never "unchanged" by default.
    keep = FR.gate(con, life_home, now_dt, keep, out)
    if not keep:
        return out
    if True:    # relevance.py (always on): the one decision, after every quiet-hour gate above; only ever fewer or later items
        keep = __import__('relevance').screen(con, life_home, now_dt, keep, out)
        if not keep:
            return out
    if (life_home / 'miss-review-on').exists():   # missreview.py: the same items, most important to the owner this week first (urgent ones stay first); the same list without the switch or a fresh order
        keep = __import__('missreview').order(life_home, keep, now_dt)
    out['waiting_their_turn'] = len(keep) - 1    # one item per text; the rest stay ready for a later run
    keep = most_pressing(keep, con, now_dt)
    told_urgent = keep[0][0]['urgency'] == 'now'
    items = [C.item_view(r['key'], opp) for r, opp in keep]
    if (life_home / 'timer-truth-on').exists():   # timers.py: a timed item going out more than 15 minutes late is handed one fact (how late, why); the same writer when nothing is late
        compose = __import__('timers').composing(con, life_home, now_dt, compose, keep, out)
    compose = __import__('recover').composing(con, life_home, now_dt, compose, keep, out)   # recover.py / capability.py (switches recover-on, capability-on): facts about what could not be finished or read; the same writer with neither switch
    compose = FR.composing(con, life_home, now_dt, compose, keep, out)   # fresh.py: the same writer unless its switch is on
    compose = __import__('relevance').composing(compose, keep)    # relevance.py: an ask that replaces an earlier one is handed what changed, as one plain fact (always on)
    result = compose(items, now_dt, recent_texts(con, now_ts, include_logged=not live))
    text = result.get('text')
    if result.get('silent'):
        # Silence can close finished items. It can never close a real question, something urgent, or a reminder the owner is
        # owed: those go out as plain facts instead.
        asks = [(r, o) for r, o in keep if (o.get('packet') or {}).get('decision') or r['urgency'] == 'now'
                or str((o.get('packet') or {}).get('headline') or '').startswith('Reminder:')]
        for r, opp in keep:
            if (r, opp) in asks:
                continue
            if __import__('integrity').silent_held(con, life_home, r['key'], opp, now_dt, out):   # commit integrity: the writer's silence closes nothing; the item waits, open, for a receipt. False without the switch
                continue
            opp['told'] = {'at': time.time(), 'silent': True}
            con.execute("UPDATE opps SET state='done', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), time.time(), r['key']))
            out['silent'] += 1
        con.commit()
        _fails(life_home, bump=False)
        if not asks:
            return out
        keep = asks
        items = [C.item_view(r['key'], opp) for r, opp in keep]
        text = C.fallback_text(items)
        out['fallback'] = 'writer chose silence for a real question'
    elif not text:
        if _fails(life_home, bump=True) < COMPOSE_FAILS_BEFORE_FALLBACK:
            out['compose_failed'] = True         # try again next tick; counted
            out['held'] += len(keep)
            return out
        text = C.fallback_text(items)            # composer unreachable three ticks running: plain facts, nothing stuck
        out['fallback'] = 'writer unreachable three times'
    _fails(life_home, bump=False)
    if result.get('violations'):
        out['style_violations'] = result['violations']
    if result.get('check_found'):                # the second reader found something: was it fixed, is anything left?
        out['fact_check'] = {'found': len(result['check_found']), 'fixed': bool(result.get('fixed')), 'left': len(result.get('check_left') or [])}
    elif result.get('checked') is False:
        out['fact_check'] = {'unreachable': True}

    # A real question the writer left out of the text was not asked: it stays ready and goes on the next run (once).
    carried = []
    for r, opp in keep:
        if (opp.get('packet') or {}).get('decision') and not mentioned(text, opp) and not opp.get('asked_later'):
            opp['asked_later'] = {'from': r['surface_at'], 'at': time.time(), 'why': 'left out of the note'}
            con.execute("UPDATE opps SET surface_at=?, payload=?, updated=? WHERE key=? AND state='ready'",
                        (next_daytime(now_dt).isoformat(), json.dumps(opp, default=str), time.time(), r['key']))
            out['left_out'] = out.get('left_out', 0) + 1
            continue
        carried.append(r['key'])
    con.commit()
    if not carried:
        return out

    text = FR.final_text(life_home, text, keep, carried, out, now_dt)   # fresh.py: sentences code owes the owner (times worded for now), then links checked against stored ones
    key = 'note:' + now_dt.strftime('%Y-%m-%dT%H:%M') + (f':u{urgent_only}' if urgent_only else '')
    if live:
        if (life_home / 'file-delivery-on').exists():   # filedeliver.py: a prepared file of a carried item rides this same send as an attachment. Never a second send. Own guard: any trouble gives back the same line
            line = __import__('filedeliver').line_for(con, life_home, line, carried, now_dt, out)
        kind = _send(con, line, key, text, carried, clock=clock, life=life_home)
        if kind in ('deferred', 'dup', 'failed'):
            out['held'] += len(carried)
            out['send'] = kind
            if kind == 'failed':
                fell = __import__('urgentfallback').text_failed(con, life_home, key, carried, now_dt)   # switch urgent-fallback-on: an urgent text that failed gets one ticket and ONE email. Nothing without the switch
                if fell:
                    out['urgent_fallback'] = fell
            return out
        out['sent'] += kind == 'sent'
        out['unknown'] += kind == 'unknown'
    else:
        con.execute('INSERT OR REPLACE INTO outbox VALUES(?,?,?,?,?)', (key, text, 'shadow', time.time(), 'logged'))
    out['told'] = sum(1 for k in carried if _mark_told(con, k, key))
    con.commit()
    __import__('recover').after_note(con, life_home, text, carried, key, now_dt, out)   # recover.py (switch recover-on): code checks its ask is in the words that went, and records that it was asked; nothing without the switch
    __import__('relevance').told(con, life_home, now_dt, key, keep, carried, out)    # relevance.py: what this note asked and told is written down, so it is not repeated (always on)
    out['note'] = key
    out['told_urgent'] = told_urgent and out['told'] > 0
    return out
