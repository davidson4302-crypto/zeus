"""ownertime: the one place that answers "what time zone is the owner in right now?".

Every module that needs the owner's clock (policy.py, notifier.py, worker.py, quietpass.py and others) takes its
PACIFIC from here. The name is kept so their many `astimezone(PACIFIC)` lines read the same; what it means is "where
the owner is", which is the home zone (settings.OWNER_TZ) unless the owner said otherwise.

Where the answer comes from: one small file in the life folder, owner-zone.json
    {"zone": "America/Chicago", "place": "Chicago", "until": "YYYY-MM-DD", "set_at": "...", "his_words": "..."}
`until` is the last day there; the day after, the answer is the home zone again without anybody doing anything.
`from` (optional, written by travel.py) is the first day there. Before it the answer is still the home zone: a yes to
a trip ask can come the day before leaving, and quiet hours must not move while the owner is still at home. No
`from`: the setting starts at once.
No file, a file that cannot be read, or a zone name this machine does not know: the home zone. The file is written
by `life zone` (lifezone.py), never by a background job.

Switch: the file itself, and only `life zone` writes it. That command exists only with the switch file `zone-on`
(glue.py), so with every switch file absent the zone file cannot come to be. To turn this off for good while a
zone is set: say `life zone home` first, or delete owner-zone.json; removing `zone-on` alone leaves the clock where
the file says until its last day. With no owner-zone.json everything here answers with the home zone, and no
prompt changes by a single character.

What is in here
  PACIFIC / OWNER    stands in wherever a time zone object is wanted. It looks the zone up each time it is used and
                     hands back ordinary datetimes that carry the real zone, so nothing downstream sees a special object.
  zone(), label()    the real zone now, and its plain name ("Central")
  alert_open()       the earliest moment a message may reach the owner (quiet hours on the owner's clock)
  contact_hold()     the same answer for anything that leaves through Hermes, with the reply exception
  clock()            a time written out for a prompt, naming the clock it is really on
  line_asleep()      the agent's texting line is on another clock than the owner's and its morning has not come
  line_follows()     the engine's texting line counts the morning on the owner's clock (switch file zone-on)
  his_clock_line()   wraps a texting line class so its quiet-hours check runs on the owner's clock
  settle()           after the zone changes, the times already stored on open items are rewritten in the new zone
  local_times()      every time stamp in a record, already converted to where the owner is, with its weekday

Deliberately NOT moved to the travel zone (each keeps the home zone):
  lifehealth.py  it mirrors when the calendar snapshot is refreshed on this machine, which does not travel
  together.py    the nightly page's day runs 8:30pm to 8:30pm; moving it would leave a gap or an overlap on travel days
  offers.py      its times sit beside sources.sender_history, which is on the home clock
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

HOME = settings.OWNER_TZ    # the owner's home zone key
FILE = 'owner-zone.json'
LIVE_LIFE = str(settings.LIFE_HOME)   # the same default tick.py and lifecli.py use
# The agent's texting line (notifier.HermesLine) refuses every send before the morning on ITS clock, the home zone.
# Wrapped by his_clock_line (notifier._line does this), the line counts that morning, and the whole quiet window,
# where the owner is. lifezone.py also asks line_follows(): with the switch file zone-on and a trusted zone file the
# engine treats the line as following the owner's clock; otherwise it does not start a note while the home-clock
# line is asleep (line_asleep), so a note is never written only to be refused again and again.
LINE_CLOCK = HOME
LINE_DAY_START = settings.QUIET_END
DAY_START = settings.QUIET_END     # first hour (owner's clock) a message may reach the owner
DAY_END = settings.QUIET_START     # from this hour on, everything waits for DAY_START
ZONE_SWITCH = 'zone-on'    # glue.ZONE_SWITCH: the same switch file that makes `life zone` a command
NAMES = {'America/Los_Angeles': 'Pacific', 'America/Denver': 'Mountain', 'America/Phoenix': 'Arizona', 'America/Chicago': 'Central',
         'America/New_York': 'Eastern', 'Pacific/Honolulu': 'Hawaii', 'America/Anchorage': 'Alaska'}
AWAY = '{label} time, where the owner is right now (they are away from home; home is on {home} time)'
TIMED = ('pending', 'executing', 'ready')      # the item states whose stored time is still compared with the clock
MAX_LOCAL_TIMES = 24
_STAMP = re.compile(r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})')

_cache: dict = {}

def _clock() -> float:
    """The time now. Tests put their own clock here."""
    return time.time()




def life_home() -> Path:
    return Path(os.environ.get('LIFE_HOME', LIVE_LIFE))


def _file(life=None) -> Path:
    return (Path(life) if life else life_home()) / FILE


def active(life=None) -> bool:
    """The switch: is there an owner-zone.json at all? Without one nothing in this file changes anything."""
    return _file(life).exists()


def rule(life=None):
    """What the file says -> {'zone': ZoneInfo, 'place', 'until': date | None, 'ends': epoch seconds | None, ...},
    or None when there is no file or it cannot be trusted (then the answer is the home zone). Read again only when the
    file changes."""
    global _cache
    p = _file(life)
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache.get('key') != key:
        found = None
        try:
            d = json.loads(p.read_text())
            z = ZoneInfo(str(d['zone']))
            until = date.fromisoformat(str(d['until'])) if d.get('until') else None
            first = date.fromisoformat(str(d['from'])) if d.get('from') else None
            starts = datetime(first.year, first.month, first.day, tzinfo=z).timestamp() if first else None   # midnight there, the first day
            ends = None
            if until:      # the first moment of the day after, on the clock of the place being left
                ends = (datetime(until.year, until.month, until.day, tzinfo=z) + timedelta(days=1)).timestamp()
            found = {'zone': z, 'place': str(d.get('place') or ''), 'until': until, 'ends': ends, 'from': first, 'starts': starts,
                     'set_at': d.get('set_at'), 'his_words': d.get('his_words')}
        except Exception:
            found = None
        _cache = {'key': key, 'rule': found}
    return _cache['rule']


def zone(at=None, life=None) -> ZoneInfo:
    """The real time zone the owner is in at `at` (epoch seconds; default now). The home zone unless the file says
    otherwise and its last day has not passed."""
    r = rule(life)
    if r is None:
        return ZoneInfo(HOME)
    now = _clock() if at is None else float(at)
    if r['ends'] is not None and now >= r['ends']:
        return ZoneInfo(HOME)
    if r.get('starts') is not None and now < r['starts']:
        return ZoneInfo(HOME)          # a trip that was agreed but has not begun: still home
    return r['zone']


def _daytime_hour(hour: int) -> bool:
    """Is this hour (owner's clock) outside quiet hours? Handles a window that crosses midnight (the usual case)."""
    if DAY_START <= DAY_END:
        return DAY_START <= hour < DAY_END
    return hour >= DAY_START or hour < DAY_END


def alert_open(at: datetime, *, life=None) -> datetime:
    """Earliest time the agent may contact its owner, in the owner's local time zone."""
    when = at.timestamp()
    trip = rule(life) or {}
    changes = sorted(t for t in (trip.get('starts'), trip.get('ends')) if t is not None and t > when)
    for boundary in [*changes, None]:
        local = datetime.fromtimestamp(when, zone(at=when, life=life))
        if _daytime_hour(local.hour):
            return local
        morning = local.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
        if morning <= local:
            morning += timedelta(days=1)
        if boundary is None or morning.timestamp() < boundary:
            return morning
        # A trip starts or ends before that morning. Re-evaluate on the clock in effect then.
        when = boundary


REPLY_WINDOW = int(settings.get('quiet_hours.reply_window_seconds', 3600))   # how long after a real message from the owner an answer still counts as a reply
OWNER_TURN_FILE = 'last-owner-turn.json'  # the chat plugin stamps each real message from the owner (the same file sendmail.py reads)
UNKNOWN = float('inf')                    # contact_hold could not work out the answer: closed, until nobody knows when
HELD_BY_HERMES = '"held": true'           # what `hermes send --json` prints when Hermes parked the message instead of sending it


def contact_hold(at=None, *, reply=False, life=None):
    """May a message reach the owner now? -> None (yes), the epoch seconds it must wait until, or UNKNOWN (wait, and
    ask again later: the answer could not be worked out). For what leaves through Hermes: scheduled results, approval
    cards, `hermes send`. Nothing breaks quiet hours unless the owner messaged first.

    During quiet hours on the OWNER's clock (alert_open, the same answer the engine's own notes use) everything waits
    for the morning. One exception: `reply=True` (the caller knows this message answers the owner) AND a real message
    from the owner arrived no more than REPLY_WINDOW seconds ago. That message opens nothing for anything else: a
    scheduled result at 10:20pm still waits after a 10:15pm message. Trouble telling the time or reading the last
    message is UNKNOWN, never "yes": a late message can still be sent, a sent one cannot be taken back."""
    try:
        now = _clock() if at is None else float(at)
        home = Path(life) if life else life_home()
        if (home / FILE).exists() and rule(home) is None:
            return UNKNOWN                       # a zone file that cannot be trusted: the owner's clock is not known
        moment = datetime.fromtimestamp(now, zone(at=now, life=home))
        opens = alert_open(moment, life=home)
        if opens <= moment:
            return None
        if reply:
            said = float(json.loads((home / OWNER_TURN_FILE).read_text())['at'])
            if 0 <= now - said <= REPLY_WINDOW:  # a time "in the future" is a clock mismatch, not a recent message
                return None
        return opens.timestamp()
    except Exception:
        return UNKNOWN


def away(life=None) -> bool:
    return zone(life=life).key != HOME


class _Owner(tzinfo):
    """Use it like a time zone. Every question is passed on to the real zone the owner is in at that moment, and the
    datetimes it makes (now, fromtimestamp, astimezone) carry that real zone, not this object. Only a time that had
    no zone and was given this one by `replace(tzinfo=PACIFIC)` keeps pointing here."""

    def _at(self, dt):
        z = zone()
        return z, (dt.replace(tzinfo=z) if dt is not None else None)

    def utcoffset(self, dt):
        z, local = self._at(dt)
        return z.utcoffset(local)

    def dst(self, dt):
        z, local = self._at(dt)
        return z.dst(local)

    def tzname(self, dt):
        z, local = self._at(dt)
        return z.tzname(local)

    def fromutc(self, dt):
        z = zone()
        return z.fromutc(dt.replace(tzinfo=z))

    @property
    def key(self) -> str:
        return zone().key

    def __repr__(self):
        return 'ownertime.OWNER'

    def __reduce__(self):
        return (_the_owner, ())


def _the_owner():
    return OWNER


OWNER = PACIFIC = _Owner()


def _real(tz):
    """The real zone behind a datetime's tzinfo, or None for a bare offset (then nothing can be said about its name)."""
    if isinstance(tz, _Owner):
        return zone()
    return tz if isinstance(tz, ZoneInfo) else None


def now() -> datetime:
    return datetime.now(OWNER)


def label(z=None) -> str:
    """The zone's everyday name: 'Pacific', 'Central', 'Eastern', or the city for anything rarer ('London')."""
    key = (z or zone()).key
    return NAMES.get(key) or key.split('/')[-1].replace('_', ' ')


def clock(dt: datetime, fmt: str) -> str:
    """dt.strftime(fmt) for a prompt whose format ends in the placeholder word Pacific (the word callers write for
    "the home clock"). At home the word becomes the home zone's name ("Pacific" for a Pacific home, so nothing changes
    there). When the time is on another clock, the word is replaced by the truth, so a model is never told
    "11:05AM Pacific" about 11:05 somewhere else."""
    text = dt.strftime(fmt)
    z = _real(dt.tzinfo)
    if 'Pacific' not in fmt:
        return text
    head, _, tail = text.rpartition('Pacific')
    if z is None or z.key == HOME:
        return head + label(ZoneInfo(HOME)) + tail
    return head + AWAY.format(label=label(z), home=label(ZoneInfo(HOME))) + tail


def said_where(at=None) -> str:
    """The words that follow a written-out time in the quiet pass's prompt. At home: the home zone's name. Away: the
    real clock, and the offset to use, because that prompt asks the model for times "with the offset"."""
    z = zone(at)
    if z.key == HOME:
        return ' ' + label(z)
    off = datetime.fromtimestamp(float(at if at is not None else _clock()), z).strftime('%z')
    return (' ' + AWAY.format(label=label(z), home=label(ZoneInfo(HOME))) + '. Every time in this conversation is on that clock: give "due" with the offset '
            + off[:3] + ':' + off[3:] + ', not the home offset')


def line_asleep(now_dt: datetime) -> bool:
    """True when the owner is on another clock and the agent's texting line would still refuse a send (before the
    morning on the line's own clock). Never true at home, with no file, or while the line follows the owner's clock
    (line_follows): there the callers' own morning test already says it."""
    if not LINE_CLOCK or not active() or line_follows():
        return False
    line = ZoneInfo(LINE_CLOCK)
    z = _real(now_dt.tzinfo)
    if z is not None and z.key == line.key:
        return False
    return now_dt.astimezone(line).hour < LINE_DAY_START


def line_follows() -> bool:
    """Does the engine's texting line count its morning on the owner's clock? Only with the switch file zone-on and
    a zone file that can be trusted, in the same life folder the rest of this file reads. Then "nothing before the
    morning" means the morning where the owner is, like everything else in the engine; after the file's last day the
    owner is on the home clock again, and so is the line. Without the switch (even with a zone file left behind):
    the line keeps the home-clock morning and line_asleep() holds notes for it."""
    try:
        return (life_home() / ZONE_SWITCH).exists() and rule() is not None
    except Exception:
        return False


def his_clock_line(base):
    """A subclass of the texting line class `base` whose quiet-hours check runs on the owner's clock (alert_open),
    for the whole quiet window, and is made twice: once when send() starts and again right before the transport
    starts (a journal write or attachment preparation can cross into quiet hours after the first check).

    `base` must offer: clock(), runner(argv, text, env, timeout, cwd) -> (rc, out, err), send(text) -> dict, and a
    `_closed(now)` hook that send() asks first (True = refuse as deferred). Every other behavior is the base line's."""
    import copy

    class HisClockLine(base):
        def _closed(self, now):
            at = datetime.fromtimestamp(now, zone(at=now))
            return alert_open(at) > at

        def send(self, text):
            held = {'kind': 'failed', 'deferred': True, 'error': 'quiet hours on the owner clock'}
            if self._closed(self.clock()):
                return held
            # A per-call copy, so another caller never sees this call's transport wrapper.
            call = copy.copy(self)
            original = self.runner
            hit_cutoff = parked = False

            def run(*args, **kwargs):
                nonlocal hit_cutoff, parked
                if self._closed(self.clock()):
                    hit_cutoff = True
                    raise OSError('quiet hours before transport started')
                answer = original(*args, **kwargs)
                # A Hermes build with its own quiet-hours hold may park the note and send it once in the morning.
                parked = HELD_BY_HERMES in str(answer[1] if isinstance(answer, (tuple, list)) and len(answer) > 1 else '')
                return answer
            call.runner = run
            result = base.send(call, text)
            if parked:      # it WILL go, once, without us: so never "failed" (that would send it a second time)
                return {'kind': 'unknown', 'held': True, 'error': 'Hermes is holding it for quiet hours and sends it once in the morning'}
            return held if hit_cutoff else result

    return HisClockLine


# ---- times already stored on open items --------------------------------------------------------------------------------

def _moment(value, z):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=z)


def _held_for_morning(opp: dict, decided) -> bool:
    """Was this morning time put there by the engine as "the owner's next morning", going by what is stored on the
    item? Yes for a morning note, for something held overnight, for a question moved to the next morning, and for a
    time the noticer asked for that code then held to the morning."""
    d = opp.get('decision') if isinstance(opp.get('decision'), dict) else {}
    if d.get('channel') == 'morning_brief' or opp.get('held_overnight') or opp.get('asked_later'):
        return True
    asked = _moment(opp.get('surface_at'), decided.tzinfo) if opp.get('surface_at') else None
    return asked is not None and asked != decided


def restamp(stored, opp: dict, z: ZoneInfo):
    """One stored time, rewritten for the zone the owner is in now -> the new text, or None when it is already right
    or cannot be read. The moment stays the same, with one exception: a morning time that only ever meant "the next
    morning" moves to that morning hour where the owner is now, including when a flight or deadline falls earlier."""
    t = _moment(stored, None)
    if t is None or t.tzinfo is None:
        return None
    local = t.astimezone(z)
    if t.utcoffset() == local.utcoffset():
        return None
    new = local
    if (t.hour, t.minute, t.second, t.microsecond) == (DAY_START, 0, 0, 0) and _held_for_morning(opp, t):
        new = datetime(t.year, t.month, t.day, DAY_START, tzinfo=z)
    return new.isoformat()


def settle(con, life=None) -> dict:
    """Call at the start of a tick (and right after `life zone`). The engine finds what is due by comparing stored
    time TEXT, which only works while every stored time is written on the same clock. So when the owner's zone has
    changed (one was set, or its last day passed overnight), the times of open items are rewritten on the new clock.
    Nothing else about an item changes. With no owner-zone.json this returns at once."""
    if not active(life):
        return {}
    z = zone(life=life)
    moved = []
    marks = ','.join('?' * len(TIMED))
    for key, state, stored, payload in [tuple(r) for r in con.execute(f'SELECT key,state,surface_at,payload FROM opps WHERE state IN ({marks})', TIMED).fetchall()]:
        try:
            opp = json.loads(payload)
        except ValueError:
            continue
        new = restamp(stored, opp if isinstance(opp, dict) else {}, z)
        if new is None or not isinstance(opp, dict):
            continue
        if isinstance(opp.get('decision'), dict) and opp['decision'].get('surface_at') == stored:
            opp['decision']['surface_at'] = new
        if isinstance(opp.get('held_overnight'), dict) and opp['held_overnight'].get('until') == stored:
            opp['held_overnight']['until'] = new
        opp['zone_moved'] = {'from': stored, 'to': new, 'at': datetime.fromtimestamp(_clock(), z).isoformat(timespec='seconds')}
        con.execute('UPDATE opps SET surface_at=?, payload=? WHERE key=? AND state=? AND surface_at=?',
                    (new, json.dumps(opp, default=str), key, state, stored))
        moved.append(key)
    if moved:
        con.commit()
    return {'zone': z.key, 'moved': len(moved)} if moved else {}


# ---- ready answers for a model ----------------------------------------------------------------------------------------

def _said(t: datetime, this_year: int, name: str) -> str:
    hour = t.hour % 12 or 12
    text = '%s %s %d, %d:%02d%s %s' % (t.strftime('%A'), t.strftime('%b'), t.day, hour, t.minute, 'am' if t.hour < 12 else 'pm', name)
    return text if t.year == this_year else text.replace(', ', ' %d, ' % t.year, 1)


def local_times(*things, limit: int = MAX_LOCAL_TIMES, life=None) -> dict:
    """Every time stamp found in the given text or records that names its own offset ("...T15:00:00-06:00", "...Z"),
    as {stamp exactly as written: 'Friday Nov 13, 1:00pm Pacific'} on the clock where the owner is now. First come, first
    kept, at most `limit`. A date with no time, or a time with no offset, is left out: code cannot know what it means."""
    z = zone(life=life)
    name, year = label(z), datetime.fromtimestamp(_clock(), z).year
    out = {}
    for thing in things:
        if thing is None:
            continue
        text = thing if isinstance(thing, str) else json.dumps(thing, default=str, ensure_ascii=False)
        for stamp in _STAMP.findall(text):
            if stamp in out:
                continue
            t = _moment(stamp, None)
            if t is None:
                continue
            out[stamp] = _said(t.astimezone(z), year, name)
            if len(out) >= limit:
                return out
    return out


def with_local_times(observation: dict, nearby=None, life=None) -> dict:
    """What the noticer is shown for one observation. With no owner-zone.json: the very same object, untouched.
    With one: a copy that also carries the times in it (and in the nearby calendar) already converted, so the model
    reads a weekday and a local clock time instead of working them out."""
    try:
        if not active(life) or not isinstance(observation, dict):
            return observation
        times = local_times(observation, nearby, life=life)
    except Exception:
        return observation          # this is only help: any trouble and the noticer gets what it always got
    if not times:
        return observation
    return {**observation, 'times_where_the_owner_is_now': {
        'how_to_use': 'Worked out by code. For the weekday and the clock time of any stamp listed here, use this and do not do the sums yourself.',
        'times': times}}


# ---- setting it ---------------------------------------------------------------------------------------------------------

def set_zone(life, zone_name: str, place: str = '', until=None, his_words: str = '', message_id=None, starts=None, asked=None) -> dict:
    """Write owner-zone.json, whole or not at all. `zone_name` must be a zone this machine knows (ZoneInfo raises if
    not). starts: the first day (a date) when that is later than today; asked: for a yes to a trip ask (travel.py),
    the ask that was answered. Both left out: the file holds only the zone, place, last day and the owner's words."""
    global _cache
    z = ZoneInfo(zone_name)
    life = Path(life)
    rec = {'zone': z.key, 'place': place or label(z), 'until': until.isoformat() if until else None,
           'set_at': datetime.fromtimestamp(_clock(), z).isoformat(timespec='seconds'), 'his_words': his_words or None, 'message_id': message_id}
    if starts:
        rec['from'] = starts.isoformat()
    if asked:
        rec['answered'] = asked
    tmp = life / (FILE + '.tmp')
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    tmp.replace(life / FILE)
    _cache = {}
    return rec
