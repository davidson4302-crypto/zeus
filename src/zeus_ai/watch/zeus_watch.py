"""zeus_watch: Zeus's one always-on watcher.

Timed watchers and wake signals are plain Python: no model turn is ever scheduled on a timer. Without this, a timer
job started the routine check every few minutes and research waited for its :56 slot, so mail that arrived just after
a check could sit for most of an hour.

What this is: ONE launchd job (KeepAlive), plain Python, a 30 second cycle, NO model call and NO send of its own. It
only decides WHEN the engine's existing steps start. It starts exactly two things, both the engine's own entry points:

  the routine check    <engine>/tick.py --max-calls 12 --email-days 2
  this hour's research <engine>/research.py run-early "<why>"                  (the hour's ONE run, pulled ahead of :56)

Pattern: routing by cheap code checks (start now / leave it to the 5-minute beat). Not used: starting a Hermes job on
demand (`hermes cron run`), which keeps a Hermes timer as the scheduler and costs a Hermes CLI start per wake; and
importing the engine into this long-lived process (a link swap would leave the old engine running here, and an
engine crash would take the watcher down with it).

When it starts the routine check (every reason is written to the log and to <life>/watcher.json):
  beat          5 minutes since the last check began: exactly what ran before, so a missed signal only costs what it cost before
  item due      a ledger item (pending or ready) whose time came, or that another process made ready, since the last check began
  read due      a flight, a watched page, a lapsed sign-in, an order or a refund whose next look came due since then
  Done tapped   the phone sign-in page logged "done: signed in" since then
  calendar      the calendar snapshot's events changed (config calendar.snapshot_file; off when not set)
  owner clock   owner-zone.json changed
  opening       the owner's quiet hours just ended (the engine's own owner clock, ownertime.alert_open)
  slot          this hour's research slot (:56) opened: the check starts the research as it always did, without waiting for a beat
When it asks for research early: a new row in the mail feed (config mail.feed_db; off when not set) whose subject
says it cannot wait (URGENT), outside quiet hours. The engine decides whether it may (research.early_slot) and it is still that hour's one run.

For an owner-requested timed work record, the same loop can hand checked saved text to the selected work plugin.
The plugin applies native delivery rules and returns it through the existing sender. The clock never starts a model.

What the routine checks never do: send, call a model, decide what is urgent enough to TEXT (research and the delivery gates do), or
get round quiet hours and no-replay: tick.py's own lock (tick.lock), research's own lock (research-run.lock) and the outbox
claim are the engine's and are not touched here. Two runners therefore cannot run the same step at once; the loser
answers "busy" and this file tries again on the next cycle.

Loud when broken: with no watcher there is no routine check and last-tick.json goes stale (a health monitor can watch
its age). launchd restarts a crashed watcher (KeepAlive). Two failed checks in a row raise one alarm a day: through
config alerts.alarm_command when set, and always as a line in <life>/watcher-alarms.jsonl and the watcher log.

No switch file.
Run: python3 -I zeus_watch.py            (launchd)        python3 -I zeus_watch.py --once   (one cycle, then exit)
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import settings  # noqa: E402


def _opt_path(env: str, key: str):
    """An optional file: env var, else config key, else None (that feature is off)."""
    v = os.environ.get(env) or settings.get(key)
    return Path(v).expanduser() if v else None


LIVE_LIFE = Path(settings.LIFE_HOME)
LIFE = Path(os.environ.get('LIFE_HOME', str(LIVE_LIFE)))
ENGINE = Path(os.environ.get('ZEUS_ENGINE', str(settings.ENGINE_DIR)))
PY = os.environ.get('ZEUS_ENGINE_PY') or str(settings.get('engine.python', '') or sys.executable)
# Optional mail feed: a SQLite file with table seen(mailbox, message_id, sender, subject, at epoch seconds). Off when unset.
INBOX_DB = _opt_path('ZEUS_INBOX_DB', 'mail.feed_db')
# Optional calendar snapshot: JSON {"events": [...]} rewritten when the calendar changes. Off when unset.
CALENDAR = _opt_path('ZEUS_CAL_SNAPSHOT', 'calendar.snapshot_file')
HERMES = Path(settings.HERMES_BIN)
ALARM_CMD = settings.get('alerts.alarm_command')                     # optional: a command (list or string) that takes --title/--log
ERRORS_LOG = LIVE_LIFE.parent / 'life-engine-errors.log'              # where the check's stderr goes

CYCLE = float(os.environ.get('ZEUS_WATCH_CYCLE', '30'))               # seconds between looks
BEAT = float(os.environ.get('ZEUS_WATCH_BEAT', '300'))                # the routine check at least this often
MIN_GAP = float(os.environ.get('ZEUS_WATCH_MIN_GAP', '15'))           # two checks never begin closer than this
EVENT_CAP = (8, 300.0)                                                # at most 8 early checks per 5 minutes: a row that keeps looking new cannot spin the engine
INBOX_LOOKBACK = 30 * 60                                              # after a restart, mail listed up to this long ago is still looked at
EARLY_RETRY = 60.0                                                    # research was busy: ask again this much later
FAILS_BEFORE_TICKET = 2                                               # failed checks in a row before one alarm
# Subjects that cannot wait for :56. This only moves WHEN the hour's research starts; whether the owner is texted is
# still research's and the delivery gates' call, so a false hit costs nothing and a miss costs only the wait for :56.
# Mostly account security notices and airline schedule changes.
URGENT = re.compile(r"\b(?:security alert|new sign[- ]?in|sign[- ]?in attempt|unusual (?:sign[- ]?in|activity)|suspicious (?:sign[- ]?in|activity|log[- ]?in)"
                    r"|new (?:log[- ]?in|device)|unrecogni[sz]ed (?:device|sign[- ]?in|log[- ]?in)|password (?:was |has been )?(?:changed|reset)"
                    r"|account (?:locked|suspended|compromised)|fraud alert"
                    r"|flight (?:\w+ ){0,7}(?:delayed|cancell?ed|canceled|changed|rebooked)|(?:delayed|cancell?ed|canceled) flight|schedule change|gate change)\b", re.I)
NOT_NEW = re.compile(r'^\s*(?:re|fwd?)\s*:', re.I)
CLOSED_STATES = ('ended', 'stopped', 'done', 'closed', 'cancelled', 'canceled')


def _epoch(value):
    """A stored time -> epoch seconds, or None."""
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def owner_clock(engine: Path):
    """The engine's own owner clock, loaded from the engine folder the link points at right now (it imports only the
    standard library). The watcher leaves when the link moves, so this is never an old engine's clock."""
    spec = importlib.util.spec_from_file_location('zeus_watch_ownertime', engine / 'ownertime.py')
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(mod)
    return mod


class Child:
    """One started engine step. Its own session, like research._spawn: stopping the watcher never cuts a check in half."""

    def __init__(self, cmd, env, out_path: Path, err_path: Path):
        self.out_path = out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w') as out, open(err_path, 'a') as err:
            self.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, cwd=tempfile.gettempdir())

    def poll(self):
        return self.proc.poll()

    def said(self) -> dict:
        try:
            lines = [ln for ln in self.out_path.read_text().splitlines() if ln.strip().startswith('{')]
            return json.loads(lines[-1]) if lines else {}
        except (OSError, ValueError):
            return {}


class Watcher:
    def __init__(self, life: Path = LIFE, engine: Path = ENGINE, *, clock=time.time, start=None, alarm=None, say=None,
                 inbox_db: Path | None = INBOX_DB, calendar: Path | None = CALENDAR, cycle: float = CYCLE, beat: float = BEAT,
                 work_home: Path | None = None, start_return=None):
        self.life, self.engine, self.clock = Path(life), Path(engine), clock
        self.engine_real = os.path.realpath(self.engine)
        self.inbox_db = Path(inbox_db) if inbox_db else None
        self.calendar = Path(calendar) if calendar else None
        self.cycle_s, self.beat = cycle, beat
        self.start = start or self._start
        self.work_home = work_home if work_home is not None else (
            self.life.parent.parent if self.life.resolve() == LIVE_LIFE.resolve() else None)
        self.start_return = start_return or self._start_return
        self.returns = {}
        self.alarm = alarm or self._alarm
        self.say = say or (lambda line: print(line, flush=True))
        self.OT = owner_clock(Path(self.engine_real))
        self.tick = self.research = None            # the running children, if any
        self.tick_began = 0.0                       # when this watcher last began a check
        self.pending = []                           # reasons that arrived while a check was running or was refused as busy
        self.urgent = []                            # mail that cannot wait and has not been handed to research yet
        self.early_after = 0.0
        self.event_starts = []
        self.fired = set()                          # a row or file change starts at most one early check
        self.known_due = None                       # the ledger items that were already due when the last check began (None: not looked yet)
        self.own_at = self.foreign = 0.0            # the newest recorded check this watcher started / some other runner started
        self.fail_streak, self.ticket_day = 0, ''
        self.was_quiet = None
        self.cal_sig = None
        self.cal_mtime = None
        self.zone_mtime = self._mtime(self.life / 'owner-zone.json')
        state = _json(self.life / 'watcher.json') or {}
        now = self.clock()
        self.inbox_at = max(float(state.get('inbox_at') or 0), now - INBOX_LOOKBACK) if state.get('inbox_at') else now
        self.last = {'reasons': [], 'at': None}

    # ---- the cheap looks (local files only; nothing here reaches a network) --------------------------------------------

    @staticmethod
    def _mtime(path: Path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def recorded_check(self) -> float:
        """When the newest finished routine check began, by the engine's own record (last-tick.json, its last act)."""
        return _epoch((_json(self.life / 'last-tick.json') or {}).get('at')) or 0.0

    def ledger_due(self, now: float) -> set:
        """Every ledger item (pending or ready) whose time has come, as 'item:<key>@<its time>'. What is NEW is worked out
        in cycle(): a pair that was not due when the last check began. (An item research selects is written with the
        time research STARTED, so its stamps are older than the last check; only "was it there?" tells it is new.)"""
        db = self.life / 'ledger.sqlite3'
        if not db.exists():
            return set()
        try:
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
                rows = con.execute("SELECT key, surface_at FROM opps WHERE state IN ('pending','ready') AND julianday(surface_at)<=julianday(?)",
                                   (datetime.fromtimestamp(now, timezone.utc).isoformat(),)).fetchall()
        except sqlite3.Error:
            return None                             # a busy or unreadable ledger is the beat's business, never a reason to spin
        return {f'item:{key}@{surface_at}' for key, surface_at in rows}

    def reads_due(self, now: float, since: float) -> list:
        """A flight, watched page, lapsed sign-in, order or refund whose next look came due after the last check began."""
        out = []

        def look(name, rows, ident, when='next_check'):
            for r in rows:
                if not isinstance(r, dict) or str(r.get('state') or '').lower() in CLOSED_STATES or r.get('closed'):
                    continue
                t = _epoch(r.get(when))
                if t is not None and since < t <= now:
                    mark = f'{name}:{r.get(ident)}@{r.get(when)}'
                    if mark not in self.fired:
                        out.append((mark, f'{name} read came due: {str(r.get(ident))[:40]}'))

        d = _json(self.life / 'flights.json') or {}
        look('flight', [t for t in d.get('trips') or [] if isinstance(t, dict) and t.get('state') == 'watching'], 'id')
        d = _json(self.life / 'watches.json') or {}
        look('watch', [w for w in d.get('watches') or [] if isinstance(w, dict) and w.get('state') == 'watching'], 'id')
        d = _json(self.life / 'signins.json') or {}
        sites = d.get('sites') or {}
        look('sign-in', [dict(s.get('lapse') or {}, site=k) for k, s in sites.items() if isinstance(s, dict) and s.get('lapse')], 'site')
        d = _json(self.life / 'orders.json') or {}
        look('order', list((d.get('orders') or {}).values()) if isinstance(d.get('orders'), dict) else d.get('orders') or [], 'id')
        d = _json(self.life / 'refunds.json') or {}
        look('refund', list((d.get('cases') or {}).values()) if isinstance(d.get('cases'), dict) else d.get('cases') or [], 'id')
        return out

    def done_tapped(self, since: float) -> list:
        """The phone sign-in page wrote "done: signed in" after the last check began. The page has already made the
        engine read the store's account page (signedin.check, force) before it writes this line; the check started here
        reads that record, it does not take the page's word."""
        path = self.life / 'signin-handoff.log.jsonl'
        m = self._mtime(path)
        if m is None or m <= since:
            return []
        out = []
        try:
            lines = path.read_text().splitlines()[-40:]
        except OSError:
            return []
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            at = _epoch(e.get('at')) or 0.0
            if e.get('event') == 'done' and e.get('result') == 'signed_in' and at >= since - 1:      # the log keeps whole seconds
                mark = f"done:{e.get('site')}@{e.get('at')}"
                if mark not in self.fired:
                    out.append((mark, f"Done tapped on the phone sign-in page: {e.get('site')} is signed in"))
        return out

    def calendar_changed(self) -> list:
        if self.calendar is None:
            return []                               # no calendar snapshot configured
        m = self._mtime(self.calendar)
        if m is None or m == self.cal_mtime:
            return []
        self.cal_mtime = m
        d = _json(self.calendar)
        if not isinstance(d, dict):
            return []
        sig = hashlib.sha1(json.dumps(d.get('events'), sort_keys=True, default=str).encode()).hexdigest()
        first, self.cal_sig, changed = self.cal_sig is None, sig, sig != self.cal_sig
        return [(f'calendar:{sig}', 'the calendar snapshot changed')] if changed and not first else []

    def zone_changed(self) -> list:
        m = self._mtime(self.life / 'owner-zone.json')
        if m == self.zone_mtime:
            return []
        self.zone_mtime = m
        return [(f'zone:{m}', "the owner's clock setting changed")]

    def slot_opened(self, now: float, since: float) -> list:
        """This hour's research slot (:56, 6am to 8pm on the owner's clock: research.FIRST_HOUR, LAST_HOUR, MINUTE) opened
        after the last check began. The check itself starts the research; this only stops it waiting for the next beat."""
        local = datetime.fromtimestamp(now, self.OT.zone(at=now, life=self.life))
        slot = local.replace(minute=56, second=0, microsecond=0)
        if local.minute < 56:
            return []
        t = slot.timestamp()
        if 6 <= slot.hour <= 20 and since < t <= now:
            return [(f'slot:{slot.isoformat()}', "this hour's research slot opened")]
        return []

    def quiet(self, now: float) -> bool:
        at = datetime.fromtimestamp(now, timezone.utc)
        return self.OT.alert_open(at, life=self.life) > at          # the engine's own answer to "may anything reach the owner now?"

    def urgent_mail(self, now: float) -> list:
        """New rows in the mail feed whose subject cannot wait for :56, not yet handed to research."""
        if self.inbox_db is None or not self.inbox_db.exists():
            return []                               # no mail feed configured
        try:
            with closing(sqlite3.connect(self.inbox_db.as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
                rows = con.execute('SELECT mailbox, message_id, sender, subject, at FROM seen WHERE at>? ORDER BY at', (self.inbox_at,)).fetchall()
        except sqlite3.Error:
            return []                               # an unreadable feed is the engine's health note's business (inbox_feed)
        found = []
        for mailbox, mid, sender, subject, at in rows:
            self.inbox_at = max(self.inbox_at, float(at or 0))
            subject = str(subject or '')
            if URGENT.search(subject) and not NOT_NEW.match(subject):
                found.append({'ref': f'email:{mailbox}:{mid}', 'why': f'mail that cannot wait for :56: "{subject[:70]}" ({mailbox})', 'at': float(at or now)})
        return self._not_yet_researched(found)

    def _not_yet_researched(self, mails: list) -> list:
        """Drop mail a research run has already been handed (the ledger's `seen` list has it)."""
        if not mails:
            return mails
        try:
            with closing(sqlite3.connect((self.life / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
                return [f for f in mails if not con.execute('SELECT 1 FROM seen WHERE id=?', (f['ref'],)).fetchone()]
        except sqlite3.Error:
            return mails

    def _start_return(self, ident: str):
        cmd = [str(HERMES), "zeus-work-inspect", "--deadline-pass", ident]
        errors = ERRORS_LOG if self.life.resolve() == LIVE_LIFE.resolve() else self.life / "watcher-errors.log"
        return Child(cmd, {**self._env(), "HERMES_HOME": str(self.work_home)},
                     self.life / "watcher-deadline.out", errors)

    def timed_returns(self):
        """Read existing work only, without a model or network call in the clock."""
        for key, child in list(self.returns.items()):
            code = child.poll()
            if code is not None:
                if code:
                    self.say(f"{self._t(self.clock())} timed work {key[0]} return runtime failed (exit {code}). "
                             "The work record and native journal retain the send decision.")
                del self.returns[key]
        if self.work_home is None:
            return
        path = self.work_home / "plugin-data/zeus-work/state.sqlite3"
        if not path.is_file():
            return
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
            due = db.execute("""
                SELECT id, json_extract(payload,'$.deadline.next_check') AS next_check
                FROM work
                WHERE json_extract(payload,'$.state') != 'stopped' AND next_check <= ?
                ORDER BY next_check LIMIT 8
            """, (self.clock(),)).fetchall()
        for ident, check in due:
            key = ident, check
            if key not in self.returns and len(self.returns) < 8:
                self.returns[key] = self.start_return(ident)

    # ---- starting the engine's own steps ---------------------------------------------------------------------------------

    def _env(self) -> dict:
        return dict(os.environ, HOME=str(settings.HOME), HERMES_HOME=str(settings.PROFILE_DIR), HERMES_PROFILE=settings.PROFILE,
                    HERMES_DISABLE_LAZY_INSTALLS='1', LIFE_BACKEND='hermes', LIFE_HOME=str(self.life))

    def _start(self, kind: str, why: str):
        override = os.environ.get('ZEUS_TICK_CMD' if kind == 'tick' else 'ZEUS_RESEARCH_CMD')
        if override:
            cmd, env = json.loads(override) + ([why] if kind == 'research' else []), dict(os.environ, LIFE_HOME=str(self.life))
        elif self.life.resolve() != LIVE_LIFE.resolve():
            raise RuntimeError('a sandbox life folder needs ZEUS_TICK_CMD and ZEUS_RESEARCH_CMD: the real engine is never started against a copy')
        elif kind == 'tick':
            cmd, env = [PY, '-I', str(self.engine / 'tick.py'), '--max-calls', '12', '--email-days', '2'], self._env()
        else:
            cmd, env = [PY, '-I', str(self.engine / 'research.py'), 'run-early', why], self._env()
        errors = ERRORS_LOG if self.life.resolve() == LIVE_LIFE.resolve() else self.life / 'watcher-errors.log'
        return Child(cmd, env, self.life / f'watcher-{kind}.out', errors)

    def _alarm(self, title: str, detail: str) -> None:
        if self.life.resolve() != LIVE_LIFE.resolve():
            return                                   # a sandbox never raises real alarms
        row = {'at': self._t(self.clock()), 'kind': 'life_tick_failed', 'title': title, 'detail': detail[:300]}
        try:
            with open(self.life / 'watcher-alarms.jsonl', 'a') as fh:
                fh.write(json.dumps(row) + '\n')
        except OSError:
            pass
        self.say(f"{row['at']} ALARM: {title}")
        if not ALARM_CMD:
            return
        cmd = list(ALARM_CMD) if isinstance(ALARM_CMD, (list, tuple)) else str(ALARM_CMD).split()
        try:
            subprocess.run(cmd + ['--kind', 'life_tick_failed', '--where', 'life:watcher', '--title', title, '--log', detail[:300]],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _reap(self, now: float) -> None:
        if self.tick is not None and self.tick.poll() is not None:
            code, said = self.tick.poll(), self.tick.said()
            self.tick = None
            if said.get('status') != 'busy':
                self.own_at = max(self.own_at, self.recorded_check())      # the record this check left is this watcher's own
            if said.get('status') == 'busy':         # another runner held tick.lock: nothing ran, so what woke us is still owed
                self.pending = self.pending or ['the check was busy; trying again']
                self.say(f'{self._t(now)} check was busy (another runner holds the lock)')
            if code != 0:                            # a crashed phase, or tick.py saying another check has held the lock for 30 minutes
                self.fail_streak += 1
                self.say(f'{self._t(now)} check FAILED (exit {code}), {self.fail_streak} in a row')
                day = datetime.fromtimestamp(now).date().isoformat()
                if self.fail_streak >= FAILS_BEFORE_TICKET and self.ticket_day != day:
                    self.ticket_day = day
                    self.alarm("Zeus's routine check failed twice in a row", f'exit code {code}; see life-engine-errors.log and health.json')
            elif said.get('status') != 'busy':
                self.fail_streak = 0
        if self.research is not None and self.research.poll() is not None:
            said, self.research = self.research.said(), None
            if said.get('early') == 'declined':
                busy = 'already going' in str(said.get('why'))
                self.say(f"{self._t(now)} early research declined: {said.get('why')}" + ('; asking again' if busy else '; the hourly run will take it'))
                if not busy:
                    self.urgent = []                 # the engine said no for this hour: the :56 run sees the mail as new, as it always did
                self.early_after = now + EARLY_RETRY
            else:
                self.urgent = []
                self.pending = self.pending or ['early research finished']      # what it selected goes through the check's gates now, not at the next beat
                self.say(f"{self._t(now)} early research finished: slot {said.get('slot')} status {said.get('status') or 'ran'}")

    @staticmethod
    def _t(now: float) -> str:
        return datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')

    def cycle(self) -> dict:
        """One look. -> what it did (also kept in watcher.json)."""
        now = self.clock()
        did = {'at': now, 'check': None, 'research': None}
        self._reap(now)
        on_file = self.recorded_check()
        since = max(on_file, self.tick_began)        # when the newest check began (one another runner started counts too)
        due = self.ledger_due(now)
        if due is not None and self.tick is None and (self.known_due is None or on_file > max(self.own_at, self.foreign)):
            # just started, or a check this watcher did not start has run: what is due right now was that check's to handle
            self.known_due, self.foreign = set(due), on_file
        new_items = sorted(m for m in (due or ()) if m not in (self.known_due or ()) and m not in self.fired)
        found = [(m, 'item came due: ' + m[5:].rsplit('@', 1)[0][:60]) for m in new_items]
        found += self.reads_due(now, since) + self.done_tapped(since) + self.calendar_changed() + self.zone_changed() + self.slot_opened(now, since)
        quiet = self.quiet(now)
        if self.was_quiet and not quiet:
            found.append((f'open:{int(now)}', "the owner's quiet hours just ended"))
        self.was_quiet = quiet
        reasons = [why for _, why in found] + self.pending
        beat = now - since >= self.beat
        if beat:
            reasons.append('5-minute beat')
        self.event_starts = [t for t in self.event_starts if now - t < EVENT_CAP[1]]
        may_start = self.tick is None and now - since >= (0 if beat else MIN_GAP) and (beat or len(self.event_starts) < EVENT_CAP[0])
        if len(self.fired) > 5000:
            self.fired.clear()                       # only a second guard: a mark cannot come back once its time is behind the last check
        self.fired.update(mark for mark, _ in found)
        if reasons and may_start:
            self.known_due = set(due) if due is not None else self.known_due      # what this check gets to handle
            self.tick, self.tick_began, self.pending = self.start('tick', '; '.join(reasons)), now, []
            if not beat:
                self.event_starts.append(now)
            did['check'] = reasons
            self.say(f"{self._t(now)} check started: {'; '.join(reasons)[:300]}")
        elif found:
            # a check is running (it may already be past that step), or one began seconds ago, or the cap is reached:
            # what was just seen is still owed, and goes with the next check
            self.pending = list(dict.fromkeys(self.pending + [why for _, why in found]))[-12:]
        # mail that cannot wait: this hour's research, now. Mail in quiet hours is left for the morning run and the opening.
        new = self.urgent_mail(now)
        if new:
            self.urgent = (self.urgent + new)[-20:]
            if quiet:
                self.say(f"{self._t(now)} {len(new)} urgent mail in quiet hours: nothing is started now; the morning run or the end of quiet hours takes it")
        if self.urgent and self.research is None and now >= self.early_after and not quiet:
            self.urgent = self._not_yet_researched(self.urgent)
        if self.urgent and self.research is None and now >= self.early_after and not quiet:
            why = self.urgent[0]['why']
            self.research, self.early_after = self.start('research', why), now + EARLY_RETRY
            did['research'] = why
            self.say(f'{self._t(now)} early research asked: {why}')
        self.last = {'at': now, 'reasons': did['check'] or [], 'research': did['research']}
        self._save(now)
        return did

    def _save(self, now: float) -> None:
        note = {'pid': os.getpid(), 'at': now, 'at_text': self._t(now), 'engine': self.engine_real, 'inbox_at': self.inbox_at, 'cycle_seconds': self.cycle_s,
                'check_running': self.tick is not None, 'research_running': self.research is not None, 'fail_streak': self.fail_streak,
                'last_reasons': self.last.get('reasons'), 'last_research': self.last.get('research')}
        tmp = self.life / '.watcher.json.tmp'
        try:
            tmp.write_text(json.dumps(note))
            os.replace(tmp, self.life / 'watcher.json')
        except OSError:
            pass

    def engine_moved(self) -> bool:
        return os.path.realpath(self.engine) != self.engine_real


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    w = Watcher()
    stop = {'now': False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.__setitem__('now', True))
    w.say(f'{w._t(time.time())} watcher started (pid {os.getpid()}, engine {w.engine_real}, life {w.life}, cycle {w.cycle_s:g}s)')
    while not stop['now']:
        try:
            w.timed_returns()
        except (OSError, sqlite3.Error) as e:
            w.say(f"Timed work clock could not read the saved state: {type(e).__name__}")
        try:
            w.cycle()
        except Exception as e:                       # one bad look never ends the watcher; a watcher that cannot look at all goes quiet and last-tick.json goes stale
            w.say(f'{w._t(time.time())} cycle error: {type(e).__name__}: {str(e)[:200]}')
        if '--once' in args:
            w.timed_returns()
            break
        if w.engine_moved() and w.tick is None and w.research is None:
            w.say(f'{w._t(time.time())} the engine link moved: leaving so launchd starts me again on the new engine')
            return 0
        end = time.time() + w.cycle_s
        while not stop['now'] and time.time() < end:
            try:
                w.timed_returns()
            except (OSError, sqlite3.Error) as e:
                w.say(f"Timed work clock could not read the saved state: {type(e).__name__}")
            time.sleep(min(1.0, max(0.0, end - time.time())))
    w.say(f'{w._t(time.time())} watcher stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
