"""commit: every job Zeus accepts, saved as ONE commitment with a stable id and a version.

Why: when a job such as "track a relative's flight" keeps its facts only in its title and in notes, a correction
("it lands at 8:40pm, not in the morning") lands in a note, the item still says "AM assumed", and a background job
started before the correction can close the item with the old facts.

A commitment is the job itself: what, when, what is watching, what proves it done, what is still not confirmed.
  identity   commitment_id ("c_" + 12 hex). The item's key never changes, whatever a correction changes.
  version    revision. A correction adds one, and so does a close. Bookkeeping (a job booked, a watch added, the
             next step set, evidence noted) does not: the version means "what was asked", nothing else.
  where      the whole object is payload['commitment'] of its item in the ledger; the table `commitments` is the index
             and the version fence; `commit_events` is the history (who, when, before, after, which chat turn) and
             holds the object after every change; `commit_ops` makes every write safe to repeat (the same op_id gets
             the same answer and nothing is done twice).
  fence      a background job carries the id and version it was started for. A result for an older version closes
             and rewrites nothing; the item goes back for its current version (worker.py's hooks).
  by code    an "arm the monitor" step that is due is carried out by due_actions (tick.py), with no model: a watch is
             added for each page, renewed before it ends, and checked against its commitment on every run.

Rules this file keeps:
- Every write is one SQLite transaction (BEGIN IMMEDIATE) over the index row, the item and the history row.
- A time is never guessed. A time needs its offset. A time whose AM/PM is not known is refused, or, only when the
  caller says so in so many words, saved as the earlier reading with the doubt written into subject.unresolved.
- Authority is never invented. "approved" anywhere in a request is dropped and reported back as ignored.
- A write that cannot be carried out changes nothing and says why in plain words (Refused), or hands back the current
  version (Conflict), so the caller reads again and works the change out again. Nothing is ever written over blind.

Switch files in the life folder:
  commit-on         commitments are written, `life commit ...` works, the human commands keep them in step, and the
                    tick runs due_actions.
  commit-on   background results are checked against the version.
Without them nobody calls in here, except to create the empty tables.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import sys
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

SWITCH = 'commit-on'
FENCE_SWITCH = 'commit-on'
SCHEMA_VERSION = 1
KINDS = ('immediate', 'one_time', 'monitor', 'recurring')
PHASES = ('planned', 'running', 'waiting', 'verifying', 'done', 'stopped')
ACTORS = ('owner', 'chat', 'worker', 'tick', 'builder')
AUTHORITY = ('owner_message', 'standing_grant', 'team_rule', 'none')
SOURCES = ('chat', 'email', 'calendar', 'note', 'builder')
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')       # the same five lifecli.py calls open
HELD = 'held'                              # a parked item (rollback): a state the older engine neither lists as open nor works
FINISHED = ('done', 'moot', 'declined', 'skipped')    # a closed item: a correction to it fixes the record and nothing else
RECORD_DAYS = 30                           # a closed item older than this is not corrected; it is said again as a new thing
ENDS = {'done': ('done', 'done'), 'stopped': ('declined', 'stopped'), 'declined': ('declined', 'stopped'), 'moot': ('moot', 'stopped')}   # how it ends -> (item state, phase)
CODE_STEPS = ('arm_monitor', 'monitor_running')       # next steps only code carries out; the older engine knows neither
MAX_LEGS = 2                               # pages one job may watch (= watches.MAX_PER_ITEM)
ARM = 'arm monitor'
# The repair ticket kind for "the monitor could not be started" and "the monitor ended early". It is its own kind on
# purpose: an automatic repair that closes a watch-list ticket once its own scan of the watch list looks clean would
# close this one unread (a monitor that never started, or is already over, is in no scan). A kind with no automatic
# check stays open for whoever fixes it, like life_exec_failed does.
MISSED_KIND = 'life_monitor_missed'
ARM_TRIES, ARM_GAP, ARM_SPAN = 3, 10 * 60, 20 * 60    # a page that cannot be read: three tries, ten minutes apart, so twenty minutes in all
RENEW_WITHIN = 6 * 3600                    # a monitor this close to its end is made longer when the job wants it longer
VERBS = ('create', 'correct', 'next', 'exec', 'verify', 'resolve', 'explain', 'coverage', 'touched')
CURRENT_OP = None                          # `--op <id>` of the life command this process is running (take_op)
_FENCE_HOME = None                         # the life folder worker.run is working in (fence_home)

SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments(
  commitment_id TEXT PRIMARY KEY, opp_key TEXT UNIQUE NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
  kind TEXT NOT NULL, phase TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commit_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, commitment_id TEXT NOT NULL, revision INTEGER NOT NULL, at TEXT NOT NULL,
  kind TEXT NOT NULL, actor TEXT NOT NULL, source_ref TEXT, op_id TEXT, before TEXT, after TEXT);
CREATE INDEX IF NOT EXISTS commit_events_by_source ON commit_events(source_ref);
CREATE TABLE IF NOT EXISTS commit_ops(
  op_id TEXT PRIMARY KEY, commitment_id TEXT, op TEXT NOT NULL, at TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
_C = 'commitment_id,opp_key,revision,kind,phase,created,updated'


class Refused(ValueError):
    """A request that cannot be carried out. The message is the plain reason. Nothing was changed."""


class Conflict(Exception):
    """The job changed since the caller last read it. Carries the current version and the current object: read it,
    work the change out again, try again."""

    def __init__(self, revision, current):
        super().__init__(f'This job is at version {revision} now, not the version this change was made for. '
                         'Nothing was changed: read it again and work the change out again.')
        self.revision, self.current = revision, current


class Stale(Exception):
    """A background write that belongs to an older version of the job. Nothing was written."""


# ---- small things --------------------------------------------------------------------------------------------------------

def ensure_schema(con) -> None:
    """The four tables, made the first time anything here is used (every entry point calls this). A ledger that never
    uses commitments never gets them, so it stays exactly what it was."""
    for stmt in SCHEMA.split(';'):
        if stmt.strip():
            con.execute(stmt)


def new_id() -> str:
    return 'c_' + uuid.uuid4().hex[:12]


def home_of(con) -> Path:
    """The life folder this ledger lives in (the switches are read from there, so a test ledger never shares one)."""
    for row in con.execute('PRAGMA database_list'):
        row = tuple(row)
        if row[1] == 'main' and row[2]:
            return Path(row[2]).parent
    raise RuntimeError('this ledger has no file')


def _home(con):
    try:
        return home_of(con)
    except Exception:
        return None


def on(life) -> bool:
    try:
        return life is not None and (Path(life) / SWITCH).exists()
    except (OSError, TypeError):
        return False


def fence_on(life) -> bool:
    try:
        return life is not None and (Path(life) / FENCE_SWITCH).exists()
    except (OSError, TypeError):
        return False


def _tz():
    from ownertime import PACIFIC      # the clock the owner is on right now (their home zone unless they said otherwise)
    return PACIFIC


def _now(now=None) -> datetime:
    if hasattr(now, 'utcoffset') and hasattr(now, 'timestamp'):       # any datetime, whatever class a test's clock made it
        return now if now.tzinfo else now.replace(tzinfo=_tz())
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(float(now), _tz())
    return datetime.now(_tz())


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


def _moment(value):
    """An ISO time WITH its offset -> a datetime; anything else -> None."""
    if hasattr(value, 'utcoffset') and hasattr(value, 'timestamp'):
        return value if value.tzinfo else None
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def _plain(value) -> str:
    """'Tue Jan 15 at 12:20PM', on the owner's clock. The same wording `life watch` uses."""
    t = _moment(value)
    if t is None:
        return 'an unknown time'
    return t.astimezone(_tz()).strftime('%a %b %d at %I:%M%p').replace(' 0', ' ')


def _text(value, limit: int = 400) -> str:
    return ' '.join(str(value if value is not None else '').split())[:limit]


def _flat(value) -> str:
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


_REL = re.compile(r'\+(\d+)\s*([mhd])')


def _when(value, now: datetime, what: str = 'the time') -> datetime:
    """A time a caller gave -> a datetime with its offset, or Refused. Never a guess: a time with no offset could be
    more than one moment, so it is refused. +90m / +2h / +3d count from now."""
    raw = str(value if value is not None else '').strip()
    m = _REL.fullmatch(raw)
    if m:
        return now + timedelta(**{{'m': 'minutes', 'h': 'hours', 'd': 'days'}[m.group(2)]: int(m.group(1))})
    if not raw:
        raise Refused(f'{what[:1].upper()}{what[1:]} is missing. Nothing was saved.')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        raise Refused(f'{what[:1].upper()}{what[1:]} "{raw}" is a day with no clock time. Give the full time with its offset, '
                      'like 2030-01-15T20:40:00-05:00. Nothing was saved.')
    try:
        t = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        raise Refused(f'{what[:1].upper()}{what[1:]} "{raw}" could not be read. Use a full time with its offset, like '
                      '2030-01-15T20:40:00-05:00, or +2h / +3d from now. Nothing was saved.') from None
    if t.tzinfo is None:
        raise Refused(f'{what[:1].upper()}{what[1:]} "{raw}" has no time zone offset, so it could mean more than one moment. '
                      'Give it with its offset, like 2030-01-15T20:40:00-05:00. Nothing was saved.')
    return t


def _due(spec: dict, now: datetime):
    """The due time of a request -> (datetime or None, a line for subject.unresolved or None).
    A time whose AM/PM (or anything else about it) is not known is refused. Only when the caller says in so many words
    to use the earlier reading ("due_unsure": {"other": "<the other reading>", "use_earlier": true, "why": "..."}) is
    the earlier one saved, with the doubt written down so every reader sees it."""
    unsure = spec.get('due_unsure')
    if unsure is None and spec.get('ampm_unknown'):
        unsure = {'why': 'AM or PM is not known'}
    if spec.get('due') in (None, ''):
        if unsure:
            raise Refused('The time is not certain and no time was given, and a time is never guessed. Ask the owner. Nothing was saved.')
        return None, None
    due = _when(spec['due'], now, 'the due time')
    if not unsure:
        return due, None
    why = _text(unsure.get('why') if isinstance(unsure, dict) else unsure, 200) or 'AM or PM is not known'
    if isinstance(unsure, dict) and unsure.get('use_given') is True:      # one reading, said in so many words to be unsure: saved as given, with the doubt beside it
        return due, f'Time not confirmed: {why}. Using {_plain(due)} until the owner says.'
    if not (isinstance(unsure, dict) and unsure.get('use_earlier') is True and unsure.get('other')):
        raise Refused(f'The time is not certain ({why}), and a time is never guessed. Ask the owner which it is. To go ahead on the earlier '
                      'reading until the owner says, send both readings and say so: "due_unsure": {"other": "<the other time>", '
                      '"use_earlier": true}. Nothing was saved.')
    other = _when(unsure['other'], now, 'the other reading of the time')
    early, late = min(due, other), max(due, other)
    return early, f'Time not confirmed: {why}. Using the earlier reading ({_plain(early)}) until the owner says; the other reading is {_plain(late)}.'


def _without_approved(value, path: str = ''):
    """A request with every "approved" taken out -> (the request, where they were). The owner's yes is only ever recorded by
    code that read the owner's own message; a request cannot carry one."""
    found = []
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() == 'approved':
                found.append((path + '.' if path else '') + str(k))
                continue
            out[k], more = _without_approved(v, (path + '.' if path else '') + str(k))
            found += more
        return out, found
    if isinstance(value, list):
        out = []
        for i, v in enumerate(value):
            clean, more = _without_approved(v, f'{path}[{i}]')
            out.append(clean)
            found += more
        return out, found
    return value, found


def _ref_id(source_ref):
    if isinstance(source_ref, dict):
        return _text(source_ref.get('id'), 200) or None
    return _text(source_ref, 200) or None


def _ref_entry(source_ref, now: datetime, quote=None, kind: str = 'chat') -> dict:
    d = source_ref if isinstance(source_ref, dict) else {}
    entry = {'kind': d.get('kind') if d.get('kind') in SOURCES else kind, 'id': _ref_id(source_ref) or '', 'at': _text(d.get('at'), 40) or _iso(now)}
    said = _text(d.get('quote') or quote, 300)
    if said:
        entry['quote'] = said
    return entry


def _need(op_id, actor) -> None:
    if not _text(op_id, 200):
        raise Refused('The request has no id of its own ("op_id"), so it could not be told apart from a repeat. Nothing was changed.')
    if actor not in ACTORS:
        raise Refused(f'"{actor}" is not one of: {", ".join(ACTORS)}. Nothing was changed.')


# ---- rows ----------------------------------------------------------------------------------------------------------------

def _index(con, ref):
    """The index row for a commitment id or an item key, or None."""
    r = con.execute(f'SELECT {_C} FROM commitments WHERE commitment_id=? OR opp_key=? LIMIT 1', (str(ref), str(ref))).fetchone()
    return dict(zip(_C.split(','), tuple(r))) if r else None


def _opp(con, key):
    r = con.execute('SELECT key,state,surface_at,payload,first_seen,updated FROM opps WHERE key=?', (key,)).fetchone()
    if r is None:
        return None
    r = tuple(r)
    try:
        p = json.loads(r[3])
    except (TypeError, ValueError):
        p = {}
    return {'key': r[0], 'state': r[1], 'surface_at': r[2], 'payload': p if isinstance(p, dict) else {}, 'first_seen': r[4], 'updated': r[5]}


def _last_saved(con, cid):
    """The object as the history last saved it. Every change writes it there in the same transaction as the item."""
    for r in con.execute('SELECT after FROM commit_events WHERE commitment_id=? AND after IS NOT NULL ORDER BY seq DESC LIMIT 40', (cid,)).fetchall():
        try:
            a = json.loads(tuple(r)[0])
        except (TypeError, ValueError):
            continue
        if isinstance(a, dict) and isinstance(a.get('commitment'), dict):
            return a['commitment']
    return None


def _object(con, idx, opp):
    """The commitment object. The history's copy counts; the copy in the item is the same unless something wrote the
    item over from an older read (reconcile puts it back)."""
    saved = _last_saved(con, idx['commitment_id'])
    if saved is not None:
        return saved
    c = (opp or {}).get('payload', {}).get('commitment')
    return c if isinstance(c, dict) else None


def _view(idx, obj, opp) -> dict:
    p = (opp or {}).get('payload') or {}
    out = dict(obj or {})
    out.update(commitment_id=idx['commitment_id'], revision=idx['revision'], kind=idx['kind'], phase=idx['phase'], opp_key=idx['opp_key'],
               state=(opp or {}).get('state'), open=_live(opp, obj), title=p.get('title'),
               surface_at=(opp or {}).get('surface_at'), deadline=p.get('deadline'))
    return out


def _item_view(opp) -> dict:
    """The fields of an item that work is started from. Kept beside every correction, so they can be put back."""
    p = (opp or {}).get('payload') or {}
    return {'title': p.get('title'), 'proposed_action': p.get('proposed_action'), 'steps': p.get('steps'),
            'decision_steps': (p.get('decision') or {}).get('steps') if isinstance(p.get('decision'), dict) else None,
            'surface_at': (opp or {}).get('surface_at'), 'deadline': p.get('deadline'), 'state': (opp or {}).get('state')}


def _live(opp, obj) -> bool:
    """Is this job still being carried? Yes while its item is open. And yes when its item was simply worked and told
    (state 'done', closed by nobody) while a step only code carries out is still ahead of it or running: "track Monday's
    flight" is worked and told on Friday, and the monitor that goes with it must still be started on Monday and looked
    after until it ends. The owner's own done / drop / stop (which write `closed` on the item) end it at once, as does anything
    that closes the item as no longer needed."""
    if not opp:
        return False
    if opp['state'] in OPEN:
        return True
    na = (obj or {}).get('next_action') or {}
    return opp['state'] == 'done' and not opp['payload'].get('closed') and na.get('do') in CODE_STEPS and not na.get('gave_up')


def _open(con) -> list:
    """Every commitment that is still being carried (_live) -> [(index row, item, object)]."""
    out = []
    marks = ','.join('?' * (len(OPEN) + 1))
    for r in con.execute(f'SELECT c.commitment_id FROM commitments c JOIN opps o ON o.key=c.opp_key WHERE o.state IN ({marks}) ORDER BY c.created, c.commitment_id',
                         OPEN + ('done',)).fetchall():
        idx = _index(con, tuple(r)[0])
        opp = _opp(con, idx['opp_key']) if idx else None
        obj = _object(con, idx, opp) if opp else None
        if obj is not None and _live(opp, obj):
            out.append((idx, opp, obj))
    return out


@contextmanager
def _tx(con):
    """One write transaction. Anything the caller left open is saved first, so this one starts clean and holds the
    write lock from its first line (two writers can never both read the old version and both win)."""
    if con.in_transaction:
        con.commit()
    con.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    else:
        if con.in_transaction:
            con.commit()


def _op_seen(con, op_id):
    r = con.execute('SELECT result FROM commit_ops WHERE op_id=?', (str(op_id),)).fetchone()
    if r is None:
        return None
    try:
        return json.loads(tuple(r)[0])
    except (TypeError, ValueError):
        return {}


def _op_done(con, op_id, cid, op: str, result: dict, now: datetime) -> None:
    con.execute('INSERT INTO commit_ops(op_id,commitment_id,op,at,result) VALUES(?,?,?,?,?)',
                (str(op_id), cid, op, _iso(now), json.dumps(result, ensure_ascii=False, default=str)))


def _op_for(con, wanted, cid) -> str:
    """The id a hook writes under: the command's own `--op` the first time, that id plus the commitment for anything
    more the same command touches (mute closes several items), a fresh one when the command gave none."""
    if not wanted:
        return uuid.uuid4().hex
    if con.execute('SELECT 1 FROM commit_ops WHERE op_id=?', (str(wanted),)).fetchone() is None:
        return str(wanted)
    return f'{wanted}:{cid}'


def _event(con, cid, revision, kind: str, actor: str, source_ref, op_id, before, after, now: datetime) -> None:
    con.execute('INSERT INTO commit_events(commitment_id,revision,at,kind,actor,source_ref,op_id,before,after) VALUES(?,?,?,?,?,?,?,?,?)',
                (cid, int(revision), _iso(now), kind, actor, _ref_id(source_ref), op_id,
                 json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                 json.dumps(after, ensure_ascii=False, default=str) if after is not None else None))


def _put(con, idx, opp, obj, now: datetime, state=None, surface_at=None, touch: bool = True) -> None:
    """The object into its item and the index row, in the transaction the caller holds. touch=False leaves the item's
    `updated` time exactly as it was: giving an item its first version adds payload['commitment'] and nothing else, so
    it must not look like the item changed (the chat list's order, the timing compare-and-set and "what did Zeus say
    last" all read that time)."""
    p = opp['payload']
    p['commitment'] = obj
    sets, vals = (['payload=?', 'updated=?'], [json.dumps(p, default=str), now.timestamp()]) if touch else (['payload=?'], [json.dumps(p, default=str)])
    if state:
        sets.append('state=?')
        vals.append(state)
    if surface_at:
        sets.append('surface_at=?')
        vals.append(surface_at)
    con.execute(f"UPDATE opps SET {', '.join(sets)} WHERE key=?", (*vals, opp['key']))
    con.execute('UPDATE commitments SET revision=?, kind=?, phase=?, updated=? WHERE commitment_id=?',
                (int(obj['revision']), obj['kind'], obj['phase'], _iso(now), idx['commitment_id']))


def _need_open(con, ref):
    idx = _index(con, ref)
    if idx is None:
        raise Refused(f'No job is saved under "{_text(ref, 120)}". Nothing was changed.')
    opp = _opp(con, idx['opp_key'])
    obj = _object(con, idx, opp)
    if not _live(opp, obj):
        raise Refused(f"That job is closed ({opp['state'] if opp else 'its item is gone'}), so it was not changed. If the owner wants it again, save it as a new job.")
    return idx, opp, obj


def _check_version(idx, obj, opp, expected, actor) -> None:
    if expected is None:
        if actor != 'owner':
            raise Refused('This change must say which version of the job it was made for ("expected_revision"). Only the owner\'s own word '
                          'goes through without one. Nothing was changed.')
        return
    try:
        same = int(expected) == int(idx['revision'])
    except (TypeError, ValueError):
        same = False
    if not same:
        raise Conflict(idx['revision'], _view(idx, obj, opp))


# ---- reading -------------------------------------------------------------------------------------------------------------

def get(con, cid_or_opp_key):
    """The commitment object, plus where its item stands: opp_key, state, open, title, surface_at, deadline. None when
    nothing is saved under that id or key."""
    ensure_schema(con)
    idx = _index(con, cid_or_opp_key)
    if idx is None:
        return None
    opp = _opp(con, idx['opp_key'])
    return _view(idx, _object(con, idx, opp), opp)


def fence_ok(con, opp_key, revision) -> bool:
    """May a background result for this version touch the item? True when the item has no commitment (an item from
    before commitments works as it always did) or the version is the current one. A job that carries no version was
    started before its item had one: it stands only while nothing was corrected since."""
    try:
        idx = _index(con, opp_key)
    except sqlite3.OperationalError:
        return True                              # no table at all: nothing here has a version
    if idx is None:
        return True
    if revision is None:
        return int(idx['revision']) == 1
    try:
        return int(revision) == int(idx['revision'])
    except (TypeError, ValueError):
        return False


def touched_by(con, source_ref) -> list:
    """Everything created or changed with this source_ref (the chat turn id), oldest first. Each is the commitment as
    get() gives it, plus `touched`: what was done to it in that turn."""
    ensure_schema(con)
    ref = _ref_id(source_ref)
    if not ref:
        return []
    seen, out = {}, []
    for r in con.execute('SELECT commitment_id,kind,revision,at,op_id FROM commit_events WHERE source_ref=? ORDER BY seq', (ref,)).fetchall():
        cid, kind, rev, at, op = tuple(r)
        if cid not in seen:
            c = get(con, cid)
            if c is None:
                continue
            seen[cid] = c | {'touched': []}
            out.append(seen[cid])
        seen[cid]['touched'].append({'kind': kind, 'revision': rev, 'at': at, 'op_id': op})
    return out


def _watch_rows(life, ids=None) -> dict:
    """-> {watch id: its record} from the life folder's watch list ({} when it cannot be read)."""
    try:
        import watches as W
        d = W.Store(life).load() if life is not None else None
    except Exception:
        d = None
    rows = {w.get('id'): w for w in (d or {}).get('watches') or [] if isinstance(w, dict)}
    return rows if ids is None else {i: rows[i] for i in ids if i in rows}


def _item_watches(life, key) -> list:
    return [w for w in _watch_rows(life).values() if w.get('item') == key and w.get('state') == 'watching']


def coverage(con, life, cid) -> dict:
    """What is really saved that will act on this job, read from the ledger and the watch list at this moment:
      one_time_due   when the item itself is worked next (None when it is closed, parked, or already worked)
      monitors       monitors that are running now: id, for, every (minutes), until, next_check
      watches        plain "tell me when it changes" watches that are running now
      arm_due        when code starts the monitor, if that step is saved and has not happened yet; arm_legs are its pages
      recurring      None (nothing repeats in this release)
    Chat reads this before it says anything is being watched."""
    ensure_schema(con)
    idx = _index(con, cid)
    if idx is None:
        raise Refused(f'No job is saved under "{_text(cid, 120)}".')
    opp = _opp(con, idx['opp_key'])
    obj = _object(con, idx, opp) or {}
    is_open = _live(opp, obj)
    listed = list((obj.get('execution_refs') or {}).get('watches') or [])
    rows = _watch_rows(life, listed)
    monitors, plain = [], []
    for wid in listed:
        w = rows.get(wid)
        if not w or w.get('state') != 'watching':
            continue
        m = w.get('monitor')
        if isinstance(m, dict):
            row = {'id': wid, 'for': w.get('for'), 'every': m.get('every'), 'until': w.get('ends_at'), 'next_check': w.get('next_check')}
            if m.get('fast_from'):
                row.update(fast_from=m.get('fast_from'), fast_every=m.get('fast_every'))
            if w.get('coverage_reduced'):
                row['coverage_reduced'] = w['coverage_reduced']
            monitors.append(row)
        else:
            plain.append({'id': wid, 'for': w.get('for'), 'until': w.get('ends_at'), 'next_check': w.get('next_check')})
    na = obj.get('next_action') or {}
    legs = [l for l in ((na.get('monitor') or {}).get('legs') or []) if not l.get('watch') and not l.get('stopped')] if na.get('do') == 'arm_monitor' else []
    arming = is_open and bool(legs) and not na.get('gave_up')
    out = {'commitment_id': idx['commitment_id'], 'opp_key': idx['opp_key'], 'revision': idx['revision'], 'open': is_open,
           'state': opp['state'] if opp else None, 'phase': idx['phase'], 'kind': idx['kind'],
           'one_time_due': opp['surface_at'] if is_open and opp['state'] in ('pending', 'executing') else None,
           'monitors': monitors if is_open else [], 'watches': plain if is_open else [],
           'arm_due': na.get('due') if arming else None,
           'arm_legs': [{k: l[k] for k in ('for', 'url', 'kind', 'every', 'starts', 'until', 'trust', 'key') if l.get(k) is not None} for l in legs] if arming else [],
           'recurring': None}
    if na.get('gave_up'):
        out['not_watching'] = str((na.get('gave_up') or {}).get('why') or 'the monitor could not be started')
    elif opp and opp['state'] == HELD:
        out['not_watching'] = 'this job is parked, so nothing is working it or watching for it'
    return out


def explain(con, life, cid) -> dict:
    """The job in plain words a model can read back to the owner: what it is, what happens next and when, what is watching
    right now, what would prove it done, what was corrected, and what is still not confirmed. `summary` is all of that
    as one short paragraph. Nothing here is taken from memory: it is read from the ledger and the watch list."""
    cov = coverage(con, life, cid)
    c = get(con, cid)
    subj, na, sc = c.get('subject') or {}, c.get('next_action') or {}, c.get('success_condition') or {}
    facts = [f for f in c.get('facts') or [] if isinstance(f, dict)]
    uncertain = [str(u) for u in subj.get('unresolved') or []] + [f"{_fact_name(f)}: {_fact_value(f.get('value'))} (not confirmed)" for f in facts if not f.get('certain')]
    corrected = [_fixed(f) for f in facts if 'was' in f or f.get('name') == 'correction']
    state = c.get('state')
    if not c.get('open'):
        nxt = 'Parked: nothing is working it.' if state == HELD else f'Nothing more: it is closed ({state}).'
    elif na.get('do') == 'arm_monitor' and na.get('gave_up'):
        nxt = 'NOT watching: ' + str(na['gave_up'].get('why'))
    elif cov['arm_due']:
        n = len(cov['arm_legs'])
        nxt = f"Code starts reading {n} page{'s' if n != 1 else ''} on {_plain(cov['arm_due'])}. It is not watching yet."
    elif na.get('do') == 'monitor_running' and state not in ('pending', 'executing'):
        nxt = f"Code is reading its page{'s' if len(cov['monitors']) != 1 else ''} until {_plain(na.get('due'))}."
    else:
        nxt = {'pending': f"It is worked on {_plain(c.get('surface_at'))}.", 'executing': 'It is being worked on right now.',
               'ready': 'It is worked; the owner has not been told yet.', 'surfaced': 'The owner was told; it waits on their word.',
               'needs_attention': 'It needs attention: the work on it failed.'}.get(state, '')
    watching = cov['monitors'] + cov['watches']
    said = [f"{w.get('for')}: " + (f"read every {w['every']} min " if w.get('every') else 'looked at when due ') + f"until {_plain(w.get('until'))}, next look {_plain(w.get('next_check'))}"
            + ('; fewer looks than planned right now' if w.get('coverage_reduced') else '') for w in watching]
    parts = [f"Job: {subj.get('what') or c.get('title')}.", nxt]
    parts.append('Watching now: ' + '; '.join(said) + '.' if said else 'Nothing is watching it right now.')
    if sc.get('outcome'):
        parts.append(f"Done when: {sc['outcome']}" + (f" (proof: {sc['evidence']})" if sc.get('evidence') else '') + '.')
    if c.get('evidence_refs'):
        parts.append('Checked: ' + '; '.join(str(e.get('what')) for e in c['evidence_refs'] if isinstance(e, dict))[:300] + '.')
    if corrected:
        parts.append('Corrected since first asked: ' + '; '.join(corrected) + '.')
    if uncertain:
        parts.append('Not confirmed: ' + '; '.join(uncertain) + '.')
    out = {'commitment_id': c['commitment_id'], 'opp_key': c['opp_key'], 'revision': c['revision'], 'kind': c['kind'], 'phase': c['phase'],
           'state': state, 'open': bool(c.get('open')), 'what': subj.get('what') or c.get('title'),
           'next': {'do': na.get('do'), 'due': na.get('due'), 'plain': nxt}, 'watching': watching,
           'done_when': {'outcome': sc.get('outcome') or '', 'evidence': sc.get('evidence') or ''},
           'verified': list(c.get('evidence_refs') or []), 'corrected': corrected, 'uncertain': uncertain,
           'summary': ' '.join(x for x in parts if x)}
    return __import__('outcome').explained(con, life, c, out)   # outcome build (switch own-outcome-on): an open job also says its owner, what it waits on and when it is looked at next. The same view without the switch


def _fact_name(f: dict) -> str:
    return str(f.get('name') or '').replace('_', ' ')


def _fixed(f: dict) -> str:
    """A corrected fact in plain words: "arrival is Tue Jan 15 at 8:40PM, not Tue Jan 15 at 8:40AM", or, for a correction
    the owner gave as a sentence, the sentence itself."""
    if f.get('name') == 'correction':
        return _text(f.get('value'), 300).rstrip('.')
    return f"{_fact_name(f)} is {_fact_value(f.get('value'))}, not {_fact_value(f.get('was'))}"


def _fact_value(v) -> str:
    return _plain(v) if _moment(v) is not None else _text(v, 120)


# ---- the parts of a commitment -------------------------------------------------------------------------------------------

def _subject(spec: dict, what: str) -> dict:
    s = spec.get('subject') if isinstance(spec.get('subject'), dict) else {}
    ids = s.get('identifiers') if isinstance(s.get('identifiers'), dict) else {}
    ids = {_text(k, 40): _text(v, 120) for k, v in ids.items() if _text(k, 40) and _text(v, 120)}
    doubts = [_text(u, 300) for u in s.get('unresolved') if _text(u, 300)][:10] if isinstance(s.get('unresolved'), list) else []
    return {'what': what, 'identifiers': ids, 'unresolved': doubts}


def _facts(raw, source_ref, now: datetime, by=None) -> list:
    """Facts from a request. `by` is who sent the request (owner, chat ...): it is written by code from the actor, never
    taken from the request, so a fact "from the owner" is one that came in with the owner's own verified words."""
    out = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not _text(f.get('name'), 60):
            continue
        v = f.get('value')
        out.append({'name': _text(f['name'], 60), 'value': v if isinstance(v, (int, float, bool)) else _text(v, 300),
                    'source_ref': _text(f.get('source_ref'), 200) or _ref_id(source_ref), 'certain': f.get('certain') is True,
                    'at': _text(f.get('at'), 40) or _iso(now)} | ({'by': by} if by else {}))
    return out[:30]


def _authority(raw) -> dict:
    a = raw if isinstance(raw, dict) else {}
    kind = a.get('kind') if a.get('kind') in AUTHORITY else 'none'
    return {'kind': kind, 'ref': _text(a.get('ref'), 200) if kind != 'none' else ''}


def _success(raw, now: datetime, base=None) -> dict:
    out = dict(base or {'outcome': '', 'evidence': ''})
    if isinstance(raw, dict):
        for k in ('outcome', 'evidence'):
            if k in raw:
                out[k] = _text(raw[k], 400)
        if raw.get('until') not in (None, ''):
            out['until'] = _when(raw['until'], now, 'the time the job should be watched until').isoformat()
    return out


def _leg_id(leg: dict) -> str:
    return json.dumps([leg.get('kind') or 'web', leg.get('url'), leg.get('part')], sort_keys=True, default=str)


def _legs(life, raw, now: datetime, due) -> list:
    """The pages of an "arm the monitor" step, each checked the way `life watch add` checks one (an address that only
    reads, a part to compare, a cadence, whose page it is). Nothing is read here. The 48 hours a monitor may run are
    counted from the moment it starts, not from now."""
    import monitor as M
    import watches as W
    if not isinstance(raw, list) or not raw:
        raise Refused('An "arm the monitor" step needs at least one page to read ("monitor": {"legs": [...]}). Nothing was saved.')
    if len(raw) > MAX_LEGS:
        raise Refused(f'One job takes at most {MAX_LEGS} pages to watch. Nothing was saved.')
    out = []
    for n, leg in enumerate(raw, 1):
        if not isinstance(leg, dict):
            raise Refused(f'Page {n} of the monitor could not be read as a page. Nothing was saved.')
        clean, why = W.validate({'for': leg.get('for'), 'read': {'kind': leg.get('kind') or 'web', 'url': leg.get('url') or ''},
                                 'part': leg.get('part') if isinstance(leg.get('part'), dict) else {}}, life)
        if clean is None:
            raise Refused(f'Page {n} of the monitor was refused: ' + '; '.join(why) + '. Nothing was saved.')
        starts = _when(leg['starts'], now, f'the start time of page {n}') if leg.get('starts') not in (None, '') else due
        if starts is None:
            raise Refused('An "arm the monitor" step needs the time to start ("due", or "starts" on each page). Nothing was saved.')
        until = _when(leg.get('until'), now, f'the end time of page {n}')
        try:
            every = int(leg.get('every'))
        except (TypeError, ValueError):
            every = 0
        if not M.MIN_EVERY <= every <= M.MAX_EVERY:
            raise Refused(f'Page {n}: how often to read it ("every") must be {M.MIN_EVERY} to {M.MAX_EVERY} minutes. Nothing was saved.')
        trust = str(leg.get('trust') or '').strip().lower()
        if trust not in M.TRUST:
            raise Refused(f'Page {n} must say whose page it is ("trust"): source (the operator\'s own page) or tracker (a third-party site). Nothing was saved.')
        if until <= starts:
            raise Refused(f'Page {n} would end before it starts. Nothing was saved.')
        if until > starts + timedelta(hours=M.MAX_AHEAD_H):
            raise Refused(f'Page {n} would be read for more than {M.MAX_AHEAD_H} hours after it starts; a monitor runs at most that long. Nothing was saved.')
        item = {'for': clean['for'], 'url': clean['read']['url'], 'kind': clean['read']['kind'], 'part': clean['part'], 'trust': trust,
                'every': every, 'until': until.isoformat()}
        if leg.get('starts') not in (None, ''):
            item['starts'] = starts.isoformat()
        if _text(leg.get('key'), 40):
            item['key'] = _text(leg['key'], 40)
        if leg.get('fast_from') or leg.get('fast_every'):
            ff = _when(leg.get('fast_from'), now, f'the faster-from time of page {n}')
            try:
                fe = int(leg.get('fast_every'))
            except (TypeError, ValueError):
                fe = 0
            if not M.MIN_EVERY <= fe <= every:
                raise Refused(f'Page {n}: the faster cadence must be {M.MIN_EVERY} minutes or more and no slower than the normal one. Nothing was saved.')
            item.update(fast_from=ff.isoformat(), fast_every=fe)
        done = leg.get('done')
        words = [w.strip().lower() for w in (done.split(',') if isinstance(done, str) else (done if isinstance(done, list) else [])) if str(w).strip()]
        if words:
            item['done'] = words[:8]
        out.append(item)
    return out


def _next_action(life, na, now: datetime) -> dict:
    if not isinstance(na, dict) or not _text(na.get('do'), 600):
        raise Refused('The next step does not say what to do ("do"). Nothing was saved.')
    do = _text(na['do'], 600)
    if do == 'monitor_running':
        raise Refused('Only code may say a monitor is running, after it has read the page. Save an "arm_monitor" step instead. Nothing was saved.')
    due = _when(na['due'], now, 'the time of the next step') if na.get('due') not in (None, '') else None
    out = {'do': do, 'due': due.isoformat() if due else None, 'depends_on': _text(na.get('depends_on'), 200) or None}
    if do == 'arm_monitor':
        legs = _legs(life, (na.get('monitor') or {}).get('legs') if isinstance(na.get('monitor'), dict) else None, now, due)
        out['due'] = min(_moment(l.get('starts')) or due for l in legs).isoformat()
        out['monitor'] = {'legs': legs}
    return out


def _settle_next(obj: dict, surface_at) -> None:
    """After pages were added, armed, stopped or dropped: the next step says what is still to do. Pages still to start
    -> arm_monitor at the earliest start. All started -> monitor_running until the last end. None left -> the job's own
    plain step again."""
    na = obj.get('next_action') or {}
    if na.get('do') not in CODE_STEPS:
        return
    legs = [l for l in ((na.get('monitor') or {}).get('legs') or []) if isinstance(l, dict)]
    waiting = [l for l in legs if not l.get('watch') and not l.get('stopped')]
    running = [l for l in legs if l.get('watch') and not l.get('stopped') and not l.get('ended')]
    if waiting:
        starts = [t for t in (_moment(l.get('starts')) or _moment(na.get('due')) for l in waiting) if t]
        na.update(do='arm_monitor', due=min(starts).isoformat() if starts else na.get('due'))
        na['monitor'] = {'legs': legs}
        obj['next_action'] = na
    elif running and not na.get('gave_up'):
        ends = [t for t in (_moment(l.get('until')) for l in running) if t]
        obj['next_action'] = {'do': 'monitor_running', 'due': max(ends).isoformat() if ends else None, 'depends_on': None, 'monitor': {'legs': legs}}
    else:
        obj['next_action'] = {'do': str((obj.get('subject') or {}).get('what') or ''), 'due': surface_at, 'depends_on': None}


def _object_from_item(life, opp, cid, now: datetime, source_ref=None, kind=None) -> dict:
    """The first version of a commitment for an item that already exists. Only what the item itself says: nothing is
    assumed about what would prove it done or on whose word it was started."""
    p = opp['payload']
    steps = ((p.get('decision') or {}).get('steps') if isinstance(p.get('decision'), dict) else None) or p.get('steps') or []
    first = next((s.get('do') for s in steps if isinstance(s, dict) and s.get('do')), None)
    what = str(p.get('proposed_action') or p.get('title') or opp['key'])
    watches = _item_watches(life, opp['key'])
    ex = p.get('exec') if isinstance(p.get('exec'), dict) else {}
    told = p.get('told') if isinstance(p.get('told'), dict) else {}
    src = {'conversation': 'chat', 'email': 'email', 'calendar': 'calendar'}.get(p.get('_source'), 'note')
    try:
        seen = _iso(datetime.fromtimestamp(float(opp['first_seen']), _tz()))
    except (TypeError, ValueError, OSError):
        seen = _iso(now)
    ref = {'kind': src, 'id': _ref_id(source_ref) or '', 'at': seen}
    if src == 'chat' and p.get('why') and p.get('why') != 'The owner mentioned it in chat.':
        ref['quote'] = _text(p['why'], 300)
    phase = {'pending': 'planned', 'executing': 'running'}.get(opp['state'], 'waiting')
    return {'schema_version': SCHEMA_VERSION, 'commitment_id': cid, 'revision': 1, 'source_refs': [ref], 'authority_ref': {'kind': 'none', 'ref': ''},
            'subject': {'what': what, 'identifiers': {}, 'unresolved': []},
            'kind': kind or ('monitor' if any(w.get('monitor') for w in watches) else 'one_time'),
            'success_condition': {'outcome': '', 'evidence': ''}, 'phase': 'running' if (watches and phase == 'planned') else phase,
            'next_action': {'do': str(first or what), 'due': opp['surface_at'], 'depends_on': None}, 'facts': [],
            'execution_refs': {'jobs': [ex['job_id']] if ex.get('job_id') else [], 'watches': [w['id'] for w in watches], 'work_id': None},
            'evidence_refs': [], 'delivery_ref': {'outbox_keys': [told['note']] if told.get('note') else []}}


def _apply_spec(life, obj: dict, spec: dict, source_ref, now: datetime) -> None:
    """The parts of a request that describe the job (never the item's own words or time: those are a correction)."""
    if isinstance(spec.get('subject'), dict):
        s = _subject(spec, obj['subject']['what'])
        obj['subject']['identifiers'].update(s['identifiers'])
        obj['subject']['unresolved'] = list(dict.fromkeys(obj['subject']['unresolved'] + s['unresolved']))
    if spec.get('facts'):
        have = {f['name'] for f in obj['facts']}
        obj['facts'] += [f for f in _facts(spec['facts'], source_ref, now) if f['name'] not in have]
    if isinstance(spec.get('success_condition'), dict):
        obj['success_condition'] = _success(spec['success_condition'], now, obj.get('success_condition'))
    if 'authority_ref' in spec:
        obj['authority_ref'] = _authority(spec['authority_ref'])
    if spec.get('kind'):
        _kind(spec['kind'])
        obj['kind'] = spec['kind']
    if spec.get('next_action'):
        obj['next_action'] = _next_action(life, spec['next_action'], now)
        if obj['next_action']['do'] == 'arm_monitor':
            obj['kind'] = 'monitor'


def _kind(kind) -> str:
    if kind not in KINDS:
        raise Refused(f'"{_text(kind, 40)}" is not a kind of job. Use one of: {", ".join(KINDS)}. Nothing was saved.')
    if kind == 'recurring':
        raise Refused('A repeating job cannot be saved yet: nothing would run it, so saving it would be a promise nobody keeps. Nothing was saved.')
    return kind


def _same_ids(a, b) -> bool:
    norm = lambda d: {_flat(k): _flat(v) for k, v in (d or {}).items() if _flat(k) and _flat(v)}
    a, b = norm(a), norm(b)
    return bool(a) and a == b


# ---- create, adopt -------------------------------------------------------------------------------------------------------

def create(con, life, spec, *, op_id, actor, source_ref=None, now=None) -> dict:
    """Save a job. -> {'status': 'created' | 'found', 'commitment': {...}} (+ 'ignored': [...] when the request carried
    an "approved"). 'found' = an OPEN job with the same item key, or the same subject.identifiers, is already saved:
    that one is handed back and nothing new is made.
    spec: what, kind (one_time unless said), due (a full time with its offset, or +2h / +3d; now when left out, as
    `life add` does), why or quote (the owner's words), subject {identifiers, unresolved}, success_condition {outcome,
    evidence}, facts [{name, value, source_ref, certain}], authority_ref, deadline, next_action, and opp_key to give an
    existing open item its commitment instead of making a new item.
    The item is made exactly as `life add` makes one: same key format, same payload, plus payload['commitment']."""
    _need(op_id, actor)
    now = _now(now)
    ensure_schema(con)
    spec, ignored = _without_approved(spec)
    with _tx(con):
        seen = _op_seen(con, op_id)
        if seen is not None:
            return seen
        result = _create(con, Path(life) if life is not None else _home(con), spec, actor, source_ref, op_id, now)
        if ignored:
            result['ignored'] = ignored
        _op_done(con, op_id, result['commitment']['commitment_id'], 'create', result, now)
    return result


def _create(con, life, spec, actor, source_ref, op_id, now: datetime) -> dict:
    if not isinstance(spec, dict):
        raise Refused('The request is not a job description. Nothing was saved.')
    if _text(spec.get('opp_key'), 300):
        return _adopt(con, life, _text(spec['opp_key'], 300), actor, source_ref, op_id, now, spec=spec)
    what = str(spec.get('what') or '').strip()
    if len(what) < 4:
        raise Refused('It does not say what the job is ("what"). Nothing was saved.')
    kind = _kind(spec.get('kind') or 'one_time')
    due, doubt = _due(spec, now)
    due = (due or now).astimezone(_tz())
    subject = _subject(spec, what)
    if doubt:
        subject['unresolved'].append(doubt)
    slug = re.sub(r'[^a-z0-9]+', '-', what.lower()).strip('-')[:48]
    key = f"chat:{slug}:{due.strftime('%Y-%m-%d')}"          # the key `life add` gives the same words on the same day
    free, taken = __import__('lifecli').free_key(con, key)    # the rule `life add` uses: an item still on the list blocks, a closed one does not (<key>~2)
    if taken:
        there, idx = _opp(con, taken), _index(con, taken)
        if idx is None:                                       # saved before commitments: it gets its first version now
            return _adopt(con, life, taken, actor, source_ref, op_id, now, spec=spec) | {'status': 'found'}
        return {'status': 'found', 'commitment': _view(idx, _object(con, idx, there), there)}
    for idx, opp, obj in _open(con):
        if _same_ids((obj.get('subject') or {}).get('identifiers'), subject['identifiers']):
            return {'status': 'found', 'commitment': _view(idx, obj, opp)}
    key = free
    step = {'do': what, 'action_class': 'prepare_or_research', 'autonomy': 'auto'}
    said = str(spec.get('why') or spec.get('quote') or '').strip()
    payload = {'title': what[:120], 'why': said or 'The owner mentioned it in chat.', 'evidence': said or what, 'urgency': 'today', 'dedupe_key': key,
               'confidence': 1.0, 'owner_involved': 'yes', '_source': 'conversation', 'steps': [step], 'proposed_action': what,
               'decision': {'autonomy': 'auto', 'surface_at': due.isoformat(), 'channel': 'text', 'steps': [step], 'granted': False,
                            'reason': 'the owner asked in chat'}}
    if spec.get('deadline') not in (None, ''):
        payload['deadline'] = _when(spec['deadline'], now, 'the deadline').isoformat()
    cid = new_id()
    obj = {'schema_version': SCHEMA_VERSION, 'commitment_id': cid, 'revision': 1, 'source_refs': [_ref_entry(source_ref, now, said)],
           'authority_ref': _authority(spec.get('authority_ref')), 'subject': subject, 'kind': kind,
           'success_condition': _success(spec.get('success_condition'), now), 'phase': 'planned',
           'next_action': {'do': what, 'due': due.isoformat(), 'depends_on': None}, 'facts': _facts(spec.get('facts'), source_ref, now, actor),
           'execution_refs': {'jobs': [], 'watches': [], 'work_id': None}, 'evidence_refs': [], 'delivery_ref': {'outbox_keys': []}}
    if spec.get('next_action'):
        obj['next_action'] = _next_action(life, spec['next_action'], now)
        if obj['next_action']['do'] == 'arm_monitor':
            obj['kind'] = kind = 'monitor'
    payload['commitment'] = obj
    ts = now.timestamp()
    con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)', (key, 'pending', 'auto', due.isoformat(), 'today', 'prepare_or_research',
                                                               json.dumps(payload), '[]', ts, ts))
    con.execute(f'INSERT INTO commitments({_C}) VALUES(?,?,?,?,?,?,?)', (cid, key, 1, kind, 'planned', _iso(now), _iso(now)))
    opp = _opp(con, key)
    _event(con, cid, 1, 'created', actor, source_ref, op_id, None, {'commitment': obj, 'item': _item_view(opp)}, now)
    return {'status': 'created', 'commitment': _view(_index(con, cid), obj, opp)}


def adopt(con, opp_key, *, op_id, actor, now=None, source_ref=None, kind=None) -> dict:
    """Give an existing open item its first commitment (version 1), built only from what the item says. Used by
    migrate, and the first time an item from before commitments is corrected or watched.
    -> {'status': 'created' | 'found', 'commitment': {...}}; 'found' when it already has one."""
    _need(op_id, actor)
    now = _now(now)
    ensure_schema(con)
    with _tx(con):
        seen = _op_seen(con, op_id)
        if seen is not None:
            return seen
        result = _adopt(con, _home(con), opp_key, actor, source_ref, op_id, now, kind=kind)
        _op_done(con, op_id, result['commitment']['commitment_id'], 'adopt', result, now)
    return result


def _adopt(con, life, key, actor, source_ref, op_id, now: datetime, spec=None, kind=None, event: str = 'adopted', closed: bool = False) -> dict:
    opp = _opp(con, key)
    if opp is None:
        raise Refused(f'No item is saved as "{_text(key, 120)}". Nothing was changed.')
    idx = _index(con, key)
    if idx is not None:
        return {'status': 'found', 'commitment': _view(idx, _object(con, idx, opp), opp)}
    if opp['state'] not in OPEN and not (closed and opp['state'] in FINISHED):
        raise Refused(f"That item is closed ({opp['state']}). A closed item is left as it is; if the owner wants it again, save it as a new job.")
    cid = new_id()
    obj = _object_from_item(life, opp, cid, now, source_ref, _kind(kind) if kind else None)
    if opp['state'] in FINISHED:                              # a closed item gets its first version only so a correction to the record has somewhere to live
        obj['phase'] = 'done' if opp['state'] == 'done' else 'stopped'
    if spec:
        _apply_spec(life, obj, spec, source_ref, now)
    con.execute(f'INSERT INTO commitments({_C}) VALUES(?,?,?,?,?,?,?)', (cid, key, 1, obj['kind'], obj['phase'], _iso(now), _iso(now)))
    idx = _index(con, cid)
    _put(con, idx, opp, obj, now, touch=False)                # a first version is not a change to the item: its `updated` time stays
    _event(con, cid, 1, event, actor, source_ref, op_id, None, {'commitment': obj, 'item': _item_view(opp)}, now)
    return {'status': 'created', 'commitment': _view(idx, obj, _opp(con, key))}


# ---- correct -------------------------------------------------------------------------------------------------------------

def correct(con, life, cid, changes, *, expected_revision, op_id, actor, source_ref=None, now=None) -> dict:
    """Change the job. -> {'status': 'corrected', 'commitment': {...}, 'retired_watches': [...], 'cancelled_approval':
    bool, 'changed': [...]} (+ 'reworked': True when a finished result was put aside so the item is worked again).
    changes: any of what (or title), due, deadline, kind, subject {identifiers, unresolved}, facts (merged by name; a
    changed value keeps the old one as `was`), success_condition, next_action, retire_watches [ids].
    In ONE transaction: the version goes up; the item's own title, steps, time and deadline are rewritten so nothing
    stale is left where work starts from; a result built on the old version (ready, told, failed) is put aside and the
    item goes back to be worked; later checks hanging off it are closed; the owner's recorded yes, and any step of this item
    that was asked or approved but not started, no longer count when something the owner's yes was bound to changed (a step
    that is being sent, or may have gone, is never touched: only reading where it went settles that).
    After the transaction the watches that no longer match are stopped. A repeat of the same op_id, or the tick's
    reconcile, finishes that if this process dies in between."""
    _need(op_id, actor)
    now = _now(now)
    ensure_schema(con)
    life = Path(life) if life is not None else _home(con)
    changes, ignored = _without_approved(changes)
    with _tx(con):
        result = _op_seen(con, op_id)
        if result is None:
            result = _correct(con, life, cid, changes, expected_revision, actor, source_ref, op_id, now)
            if ignored:
                result['ignored'] = ignored
            _op_done(con, op_id, result['commitment']['commitment_id'], 'correct', result, now)
    _stop_watches(life, result.get('retired_watches') or [], 'superseded by a correction', now)
    return result


def _stop_watches(life, ids, why: str, now: datetime) -> list:
    """Stop these watches. Stopping one that is already stopped does nothing, so this is safe to repeat."""
    done = []
    if life is None or not ids:
        return done
    import watches as W
    for wid in ids:
        try:
            if W.stop(life, wid, why, now, state='stopped'):
                done.append(wid)
        except Exception:
            pass                                 # the tick's reconcile tries again
    return done


def _reword(p: dict, olds: set, what: str) -> None:
    """The item's own words follow the correction: the title, the proposed step, and every step that carried the old
    words. Steps written by the noticer in its own words cannot be reworded by code, so they are replaced by one plain
    read-and-prepare step for the corrected job (never more authority than before, only less)."""
    hit = False
    for steps in (p.get('steps'), p['decision'].get('steps') if isinstance(p.get('decision'), dict) else None):
        for s in steps if isinstance(steps, list) else []:
            if isinstance(s, dict) and str(s.get('do') or '') in olds:
                s['do'] = what
                hit = True
    if not hit:
        step = {'do': what, 'action_class': 'prepare_or_research', 'autonomy': 'auto'}
        p['steps'] = [step]
        if isinstance(p.get('decision'), dict):
            p['decision']['steps'] = [dict(step)]
    p['title'] = what[:120]
    p['proposed_action'] = what
    if str(p.get('evidence') or '') in olds:      # an item added in chat with no words of the owner's kept its own words as the evidence: those go too
        p['evidence'] = what


def _apply_item(p: dict, item: dict):
    """Put an item's work fields back as a correction left them. -> the time for the surface_at column, or None."""
    for k in ('title', 'proposed_action', 'steps', 'deadline'):
        if item.get(k) is not None:
            p[k] = item[k]
        elif k == 'deadline':
            p.pop('deadline', None)
    if isinstance(p.get('decision'), dict):
        if item.get('decision_steps') is not None:
            p['decision']['steps'] = item['decision_steps']
        if item.get('surface_at'):
            p['decision']['surface_at'] = item['surface_at']
    return item.get('surface_at')


def _cancel_approval(con, key, p: dict, version, now: datetime) -> bool:
    """The owner's yes was for the job as it stood. -> True when a recorded yes, or a step that was asked or approved, was
    set aside. A step that is being sent or may have gone ('sending', 'unknown') is left exactly as it is."""
    hit = False
    if p.get('approved'):
        was = p.pop('approved')
        p['approved_before'] = list(p.get('approved_before') or []) + [was if isinstance(was, dict) else {'value': was}]
        hit = True
    try:
        rows = con.execute("SELECT id,state,history FROM actions WHERE item=? AND state IN ('prepared','asked','approved')", (key,)).fetchall()
    except sqlite3.OperationalError:
        return hit                               # no such table: the one-send switch was never on for this ledger
    for r in rows:
        rid, state, history = tuple(r)
        try:
            h = json.loads(history) if history else []
        except (TypeError, ValueError):
            h = []
        h.append({'state': 'replaced', 'at': now.timestamp(), 'why': f'the job was corrected (version {version}): a step prepared for the earlier version no longer counts'})
        cur = con.execute("UPDATE actions SET state='replaced', history=?, updated=? WHERE id=? AND state=?", (json.dumps(h, default=str), now.timestamp(), rid, state))
        hit = hit or (cur.rowcount == 1 and state in ('asked', 'approved'))
    return hit


def _close_children(con, key, why: str, now: datetime, keep_version=None) -> int:
    """Later checks hanging off an item ("<key>#fu1:...") that are not done yet are closed: they were scheduled from a
    result for an older version. keep_version: checks that carry this version stay."""
    n = 0
    for c in con.execute("SELECT key,payload FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready')", (key + '#fu%',)).fetchall():
        ckey, raw = tuple(c)
        try:
            cp = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if keep_version is not None and cp.get('parent_rev') == keep_version:
            continue
        cp['closed'] = {'state': 'moot', 'at': now.timestamp(), 'by': 'engine', 'why': why}
        con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=?", (json.dumps(cp, default=str), now.timestamp(), ckey))
        n += 1
    return n


def _move_children(con, key, shift, version, now: datetime) -> int:
    """Later checks hanging off an item whose time moved: one that has not started moves by the same amount (its time in
    the list, in its own decision and in what it was asked to check at), and carries the new version. One that is being
    worked or is already worked was for the old time and is closed. -> how many moved."""
    n = 0
    for c in con.execute("SELECT key,state,surface_at,payload FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready')", (key + '#fu%',)).fetchall():
        ckey, state, at, raw = tuple(c)
        try:
            cp = json.loads(raw)
        except (TypeError, ValueError):
            continue
        t = _moment(at)
        if state != 'pending' or t is None:
            cp['closed'] = {'state': 'moot', 'at': now.timestamp(), 'by': 'engine', 'why': 'superseded by a correction'}
            con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=?", (json.dumps(cp, default=str), now.timestamp(), ckey))
            continue
        to = (t + shift).isoformat()
        if isinstance(cp.get('decision'), dict):
            cp['decision']['surface_at'] = to
        if isinstance(cp.get('followup'), dict) and _moment(cp['followup'].get('when')):
            cp['followup']['when'] = (_moment(cp['followup']['when']) + shift).isoformat()
        cp['parent_rev'] = version
        con.execute('UPDATE opps SET surface_at=?, payload=?, updated=? WHERE key=?', (to, json.dumps(cp, default=str), now.timestamp(), ckey))
        n += 1
    return n


_NOTED = re.compile(r' \(Corrected: .*\)$')


def _annotate(p: dict, said: str) -> None:
    """The owner corrected something in plain words and code cannot tell which words of the item it replaces. So the item's
    reason becomes the correction itself (what it said before is kept as why_before, which no prompt reads), and every
    step carries the correction where it is read, so no reader starts work from the old words alone. A second
    correction replaces the first."""
    mark = f' (Corrected: {said})'
    for steps in (p.get('steps'), p['decision'].get('steps') if isinstance(p.get('decision'), dict) else None):
        for s in steps if isinstance(steps, list) else []:
            if isinstance(s, dict) and s.get('do'):
                s['do'] = _NOTED.sub('', str(s['do'])) + mark
    if p.get('proposed_action'):
        p['proposed_action'] = _NOTED.sub('', str(p['proposed_action'])) + mark
    if not str(p.get('why') or '').startswith('Corrected: '):
        p['why_before'] = p.get('why')
    p['why'] = 'Corrected: ' + said


def _watch_text(w) -> str:
    if not isinstance(w, dict):
        return ''
    read = w.get('read') if isinstance(w.get('read'), dict) else {}
    part = w.get('part') if isinstance(w.get('part'), dict) else {}
    return _flat(' '.join(str(x) for x in (w.get('for'), w.get('url') or read.get('url'), w.get('key') or (w.get('monitor') or {}).get('key'), *part.values())))


def _names_old(text: str, gone: list) -> bool:
    """Does this watch (or page) still name something the correction took away, and not what replaced it?"""
    return any(len(old) >= 3 and old in text and not (new and new in text) for old, new in gone)


def _correct(con, life, cid, changes, expected, actor, source_ref, op_id, now: datetime, was_due=None, quote=None) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise Refused('The request names nothing to change. Nothing was changed.')
    idx = _index(con, cid)
    item = _opp(con, idx['opp_key'] if idx else str(cid))
    if item is not None and item['state'] in FINISHED and not _live(item, _object(con, idx, item) if idx else None):
        return _correct_record(con, life, idx, item, changes, expected, actor, source_ref, op_id, now, quote)     # already finished: the record is put right, nothing else
    if idx is None and item is not None:
        _adopt(con, life, str(cid), actor, None, None, now)          # an open item from before commitments: its first version, then the correction
    idx, opp, obj = _need_open(con, cid)
    _check_version(idx, obj, opp, expected, actor)
    p, key = opp['payload'], opp['key']
    before = {'commitment': copy.deepcopy(obj), 'item': _item_view(opp)}
    new = copy.deepcopy(obj)
    subj = new.setdefault('subject', {'what': '', 'identifiers': {}, 'unresolved': []})
    subj.setdefault('identifiers', {})
    subj.setdefault('unresolved', [])
    refs = new.setdefault('execution_refs', {'jobs': [], 'watches': [], 'work_id': None})
    sub_in = changes.get('subject') if isinstance(changes.get('subject'), dict) else {}
    changed, bound, surface, shift, replaced = [], False, None, None, None

    what = str(changes.get('what') or changes.get('title') or sub_in.get('what') or '').strip()
    if what and what != subj.get('what'):
        if len(what) < 4:
            raise Refused('The new words for the job are too short to be a job. Nothing was changed.')
        olds = {str(x) for x in (subj.get('what'), p.get('title'), p.get('proposed_action')) if x}
        _reword(p, olds, what)
        if (new.get('next_action') or {}).get('do') in olds:
            new['next_action']['do'] = what
        subj['what'] = what
        changed.append('what')
        bound = True

    if changes.get('was') not in (None, '') and changes.get('due') in (None, ''):
        raise Refused('The time this one replaces ("was") only goes with the new time ("due"). Nothing was changed.')
    if changes.get('due') not in (None, '') or changes.get('due_unsure') or changes.get('ampm_unknown'):
        due, doubt = _due(changes, now)
        due = due.astimezone(_tz())
        cur = _moment(was_due or opp['surface_at'])
        if changes.get('was') not in (None, ''):
            replaced = _when(changes['was'], now, 'the time this one replaces')
        if cur is None or due != cur:
            surface = due.isoformat()
            shift = (due - cur) if cur is not None else None
            if isinstance(p.get('decision'), dict):
                p['decision']['surface_at'] = surface
            na = new.get('next_action') or {}
            if na.get('do') not in CODE_STEPS and 'next_action' not in changes:
                na['due'] = surface
                new['next_action'] = na
            elif 'next_action' not in changes:
                # The job's time moved, so the monitor that goes with it moves by the same amount: when each page starts
                # being read, until when, and from when it is read faster. A page already being read keeps running; its
                # end moves here and the renewal step makes the watch itself longer when that is later.
                # By how much is never guessed. Either the caller says which time this one replaces ("was"), or the item's
                # own time was the event's time (it lay inside the monitor's window, half a day either side). Otherwise the
                # monitor is left where it is and the job says so in plain words, where chat and the worker read it.
                legs = [l for l in ((na.get('monitor') or {}).get('legs') or []) if isinstance(l, dict) and not l.get('stopped') and not l.get('ended')]
                edges = [t for t in [_moment(l.get('starts')) or _moment(na.get('due')) for l in legs] + [_moment(l.get('until')) for l in legs] if t]
                half = timedelta(hours=12)
                if replaced is not None:
                    by = due - replaced
                elif cur is not None and edges and min(edges) - half <= cur <= max(edges) + half:
                    by = shift
                else:
                    by = None
                unmoved = "The monitor's own times were not moved by this correction"
                subj['unresolved'] = [u for u in subj['unresolved'] if not str(u).startswith(unmoved)]
                if by is not None:
                    for l in legs:
                        for k in ('starts', 'until', 'fast_from'):
                            if _moment(l.get(k)):
                                l[k] = (_moment(l[k]) + by).isoformat()
                    if _moment(na.get('due')):
                        na['due'] = (_moment(na['due']) + by).isoformat()
                elif edges and any(not l.get('watch') for l in legs):
                    subj['unresolved'].append(f'{unmoved}: it still starts {_plain(min(edges))} and ends {_plain(max(edges))}. Check that this is still right.')
                    changed.append('unresolved')
            changed.append('due')
            bound = True
        kept = [u for u in subj['unresolved'] if not str(u).startswith('Time not confirmed:')]
        if doubt:
            kept.append(doubt)
        if kept != subj['unresolved']:
            subj['unresolved'] = kept
            if 'unresolved' not in changed:
                changed.append('unresolved')

    if 'deadline' in changes:
        dl = _when(changes['deadline'], now, 'the deadline').isoformat() if changes['deadline'] not in (None, '') else None
        if dl != p.get('deadline'):
            if dl:
                p['deadline'] = dl
            else:
                p.pop('deadline', None)
            changed.append('deadline')
            bound = True

    if changes.get('kind') and changes['kind'] != new.get('kind'):
        new['kind'] = _kind(changes['kind'])
        changed.append('kind')

    old_ids = dict(subj['identifiers'])
    if isinstance(sub_in.get('identifiers'), dict):
        ids = dict(old_ids)
        for k, v in sub_in['identifiers'].items():
            k = _text(k, 40)
            if k and v in (None, ''):
                ids.pop(k, None)
            elif k:
                ids[k] = _text(v, 120)
        if ids != old_ids:
            subj['identifiers'] = ids
            changed.append('identifiers')
            bound = True
    if isinstance(sub_in.get('unresolved'), list):
        doubts = [_text(u, 300) for u in sub_in['unresolved'] if _text(u, 300)][:10]
        if doubts != subj['unresolved']:
            subj['unresolved'] = doubts
            if 'unresolved' not in changed:
                changed.append('unresolved')

    if _text(changes.get('unsure'), 300) and _text(changes['unsure'], 300) not in subj['unresolved']:
        subj['unresolved'] = subj['unresolved'] + [_text(changes['unsure'], 300)]      # something the owner was not sure of, in plain words
        if 'unresolved' not in changed:
            changed.append('unresolved')

    said = _text(changes.get('correction'), 400)                  # the corrected thing in plain words, with no field to put it in
    incoming = _facts(changes.get('facts'), source_ref, now, actor)
    if replaced is not None and changes.get('due') not in (None, ''):
        # The owner said which time the new one replaces: that is kept as a fact ("time is 8:40pm, not 8:40am"), so the worker is told
        # in so many words, and a later email or calendar entry that still carries the old time is seen for what it is.
        incoming.append({'name': 'time', 'value': _when(changes['due'], now, 'the due time').astimezone(_tz()).isoformat() if surface is None else surface,
                         'was': replaced.astimezone(_tz()).isoformat(), 'source_ref': _ref_id(source_ref), 'certain': actor == 'owner', 'at': _iso(now), 'by': actor})
    if said:
        incoming.append({'name': 'correction', 'value': said, 'source_ref': _ref_id(source_ref), 'certain': actor == 'owner', 'at': _iso(now), 'by': actor})
        _annotate(p, said)
    if incoming:
        have = {f.get('name'): f for f in new.setdefault('facts', []) if isinstance(f, dict)}
        for f in incoming:
            old = have.get(f['name'])
            if old is None:
                new['facts'].append(f)
                changed.append('fact: ' + f['name'])
                bound = True
            elif old.get('value') != f['value'] or bool(old.get('certain')) != f['certain']:
                if old.get('value') != f['value']:
                    f['was'] = old.get('value')                   # the worker and chat can say "X, not Y"
                    bound = True
                elif 'was' in old:
                    f['was'] = old['was']
                old.clear()
                old.update(f)
                changed.append('fact: ' + f['name'])

    if isinstance(changes.get('success_condition'), dict):
        sc = _success(changes['success_condition'], now, new.get('success_condition'))
        if sc != new.get('success_condition'):
            new['success_condition'] = sc
            changed.append('success_condition')

    if 'next_action' in changes:
        na = _next_action(life, changes['next_action'], now)
        if na != new.get('next_action'):
            new['next_action'] = na
            new.pop('attempts', None)                             # a new plan starts with a clean count of tries
            if na['do'] == 'arm_monitor':
                new['kind'] = 'monitor'
            changed.append('next_action')

    listed = list(refs.get('watches') or [])
    retire = [w for w in changes['retire_watches'] if w in listed] if isinstance(changes.get('retire_watches'), list) else []
    gone = [(_flat(v), _flat(subj['identifiers'].get(k))) for k, v in old_ids.items() if _flat(v) != _flat(subj['identifiers'].get(k))]
    gone += [(_flat(f['was']), _flat(f.get('value'))) for f in incoming if 'was' in f and f.get('name') != 'correction' and _moment(f['was']) is None
             and _flat(f['was']) != _flat(f.get('value'))]        # a fact whose words changed ("flight": WN 3418 -> WN 1347) names what tracking no longer matches, too
    retired_legs = []
    if 'next_action' in changed and new['next_action'].get('do') == 'arm_monitor':
        retire = list(listed)                                     # a new plan for what is watched replaces what was watched
    elif gone and listed:
        rows = _watch_rows(life, listed)
        retire += [w for w in listed if w not in retire and _names_old(_watch_text(rows.get(w)), gone)]
    na = new.get('next_action') or {}
    if 'next_action' not in changed and na.get('do') in CODE_STEPS:
        legs = [l for l in ((na.get('monitor') or {}).get('legs') or []) if isinstance(l, dict)]
        keep = [l for l in legs if not (l.get('watch') in retire or (not l.get('watch') and gone and _names_old(_watch_text(l), gone)))]
        if len(keep) != len(legs):
            retired_legs = [l.get('for') for l in legs if l not in keep]
            na['monitor'] = {'legs': keep}
            if not keep and not [w for w in listed if w not in retire]:
                subj['unresolved'] = list(dict.fromkeys(subj['unresolved'] + ['The monitor that was set up was for what has now been corrected. It was taken down and has not been set up again.']))
    if retire:
        refs['watches'] = [w for w in listed if w not in retire]
        if 'retire_watches' not in changed:
            changed.append('retire_watches')
    if not changed:
        raise Refused('Nothing in the request differs from what is saved. Nothing was changed.')
    _settle_next(new, surface or opp['surface_at'])

    state = None
    if opp['state'] in ('ready', 'surfaced', 'needs_attention', 'done'):      # 'done' only reaches here while its monitor is still ahead or running (_live)
        aside = {'version': idx['revision'], 'at': _iso(now), 'state': opp['state']}
        for k in ('packet', 'told', 'ready_at', 'asked_later', 'held_overnight', 'exec', 'attention'):
            if k in p:
                v = p.pop(k)
                if k in ('packet', 'told'):
                    aside[k] = v
        p['superseded'] = (list(p.get('superseded') or []) + [aside])[-5:]
        state = 'pending'
        if isinstance(new.get('next_action'), dict) and new['next_action'].pop('gave_up', None):
            new.pop('attempts', None)                             # a corrected job that had given up on starting its monitor tries again from the first try
        if surface is None and (_moment(opp['surface_at']) or now) < now:
            surface = now.astimezone(_tz()).isoformat()          # its time had passed: the fresh work starts on the next run, and "late" counts from now
            if isinstance(p.get('decision'), dict):
                p['decision']['surface_at'] = surface
    new['revision'] = int(idx['revision']) + 1
    cancelled = _cancel_approval(con, key, p, new['revision'], now) if bound else False
    moved = 0
    if bound and shift is not None and state is None and set(changed) <= {'due', 'deadline', 'unresolved'}:
        moved = _move_children(con, key, shift, new['revision'], now)     # only its time moved and it is not being worked again: its later checks move with it
    elif bound:
        _close_children(con, key, 'superseded by a correction', now)
    new['phase'] = 'running' if (opp['state'] == 'executing' or refs.get('watches')) else 'planned'
    if _ref_id(source_ref) or quote or changes.get('quote'):
        new['source_refs'] = (list(new.get('source_refs') or []) + [_ref_entry(source_ref, now, quote or changes.get('quote'))])[-20:]
    _put(con, idx, opp, new, now, state=state, surface_at=surface)
    after_opp = _opp(con, key)
    _event(con, idx['commitment_id'], new['revision'], 'corrected', actor, source_ref, op_id, before,
           {'commitment': new, 'item': _item_view(after_opp), 'changed': changed, 'retired_watches': retire, 'retired_legs': retired_legs,
            'cancelled_approval': cancelled}, now)
    result = {'status': 'corrected', 'commitment': _view(_index(con, idx['commitment_id']), new, after_opp), 'retired_watches': retire,
              'cancelled_approval': cancelled, 'changed': changed}
    if retired_legs:
        result['retired_legs'] = retired_legs
    if state:
        result['reworked'] = True
    if moved:
        result['moved_checks'] = moved
    return result


# ---- a correction to something already finished: the record is put right, nothing else -------------------------------------

def _day(value) -> str:
    """'Sat Jan 12', on the owner's clock."""
    t = _moment(value) if not isinstance(value, (int, float)) else datetime.fromtimestamp(float(value), _tz())
    return t.astimezone(_tz()).strftime('%a %b %d').replace(' 0', ' ') if t else 'an unknown day'


def _finished_at(item: dict):
    """When a closed item was finished: when it was closed or the owner was told, whichever is later; else when it last changed."""
    p = item['payload']
    marks = []
    for part in (p.get('closed'), p.get('told')):
        try:
            marks.append(float((part or {}).get('at')))
        except (TypeError, ValueError):
            pass
    try:
        return max(marks) if marks else float(item.get('updated') or item.get('first_seen') or 0)
    except (TypeError, ValueError):
        return 0.0


def _correct_record(con, life, idx, item, changes, expected, actor, source_ref, op_id, now: datetime, quote=None) -> dict:
    """The owner corrected a detail of something that is already finished ("no, pm", "Thursday, not Tuesday"). The chat list
    keeps a finished item the owner was told about in view for two days, with the words it was told in, so the wrong sentence
    would stay in front of the chat model. This fixes the record and nothing else:
      - the corrected fact goes on the commitment (an item that never had one gets its first version here, quietly);
      - the words the chat list reads lead with the correction. Code cannot know which words of a sentence are wrong,
        so nothing is rewritten in place: the result's headline becomes "Corrected <day>: <the correction>." (newest
        first when there are several), the result's details become "Earlier wording, not current where it differs:
        <what it said>", the reason (`why`) is led the same way, and the title is marked "(corrected)". What the result
        first said is kept untouched in packet['superseded'].
    Never: the state, the time it was due, the owner's `told` record, the owner's recorded yes, how it was closed, the prepared ask,
    its later checks, a watch or a job. A time (`due`) is written down as a fact only. Nothing is reopened or scheduled.
    A closed item older than 30 days is not corrected: it is said again as a new thing if it still matters."""
    p, key = item['payload'], item['key']
    done_at = _finished_at(item)
    if done_at and now.timestamp() - done_at > RECORD_DAYS * 86400:
        raise Refused(f'That was finished on {_day(done_at)}; say it again as a new thing if it still matters.')
    if idx is None:
        _adopt(con, life, key, actor, None, None, now, closed=True)        # quietly: its `updated` time is not touched by this
        idx, item = _index(con, key), _opp(con, key)
        p = item['payload']
    obj = _object(con, idx, item)
    _check_version(idx, obj, item, expected, actor)
    packet = p.get('packet') if isinstance(p.get('packet'), dict) else None
    before = {'commitment': copy.deepcopy(obj), 'item': _item_view(item) | {'why': p.get('why'), 'headline': (packet or {}).get('headline'), 'details': (packet or {}).get('details')}}
    new = copy.deepcopy(obj)
    subj = new.setdefault('subject', {'what': '', 'identifiers': {}, 'unresolved': []})
    subj.setdefault('unresolved', [])
    parts, changed = [], []
    said = _text(changes.get('correction'), 400)
    incoming = []                                                    # in the order they are said back: the owner's sentence, the time, then the named facts
    if said:
        incoming.append({'name': 'correction', 'value': said, 'source_ref': _ref_id(source_ref), 'certain': actor == 'owner', 'at': _iso(now), 'by': actor})
    if changes.get('was') not in (None, '') and changes.get('due') in (None, ''):
        raise Refused('The time this one replaces ("was") only goes with the new time ("due"). Nothing was changed.')
    if changes.get('due') not in (None, '') or changes.get('due_unsure') or changes.get('ampm_unknown'):
        due, doubt = _due(changes, now)                              # a finished item is not due again: its time is written down as a fact, and that is all
        due = due.astimezone(_tz())
        was = _when(changes['was'], now, 'the time this one replaces').astimezone(_tz()) if changes.get('was') not in (None, '') else None
        incoming.append({'name': 'time', 'value': due.isoformat(), 'source_ref': _ref_id(source_ref), 'certain': actor == 'owner' and not doubt, 'at': _iso(now), 'by': actor}
                        | ({'was': was.isoformat()} if was else {}))
        if doubt and doubt not in subj['unresolved']:
            subj['unresolved'].append(doubt)
    if changes.get('deadline') not in (None, ''):
        incoming.append({'name': 'deadline', 'value': _when(changes['deadline'], now, 'the deadline').astimezone(_tz()).isoformat(), 'source_ref': _ref_id(source_ref),
                         'certain': actor == 'owner', 'at': _iso(now), 'by': actor})
    incoming += _facts(changes.get('facts'), source_ref, now, actor)
    have = {f.get('name'): f for f in new.setdefault('facts', []) if isinstance(f, dict)}
    for f in incoming:
        old = have.get(f['name'])
        if old is not None and old.get('value') == f['value'] and bool(old.get('certain')) == bool(f['certain']):
            continue
        if old is not None and old.get('value') != f['value'] and 'was' not in f:
            f['was'] = old.get('value')
        if old is None:
            new['facts'].append(f)
        else:
            old.clear()
            old.update(f)
        changed.append('fact: ' + f['name'])
        parts.append(_fixed(f) if ('was' in f or f['name'] == 'correction') else f"{_fact_name(f)} is {_fact_value(f.get('value'))}")
    title = str(p.get('title') or '')
    plain_title = title[:-len(' (corrected)')] if title.endswith(' (corrected)') else title
    what = str(changes.get('what') or changes.get('title') or '').strip()
    if what and what != plain_title:
        if len(what) < 4:
            raise Refused('The new words for the job are too short to be a job. Nothing was changed.')
        subj['what'], plain_title = what, what[:120]
        changed.append('what')
        parts.append(f'it is now called "{what[:120]}"')
    unsure = _text(changes.get('unsure'), 300)
    if unsure and unsure not in subj['unresolved']:
        subj['unresolved'].append(unsure)
        changed.append('unresolved')
        parts.append('not certain: ' + unsure.rstrip('.'))
    if not parts:
        raise Refused('That correction is already on record for it. Nothing was changed.')
    text = '; '.join(parts)
    log = (list(p.get('corrections') or []) + [{'at': _iso(now), 'text': text, 'by': actor}])[-5:]
    lead = ' '.join(f"Corrected {_day(c.get('at'))}: {str(c.get('text')).rstrip('.')}." for c in reversed(log[-3:]))
    p['corrections'] = log
    if packet is not None and (packet.get('headline') or packet.get('details')):
        first = packet.setdefault('superseded', {'headline': packet.get('headline'), 'details': packet.get('details'), 'at': _iso(now)})
        earlier = ' '.join(x for x in (str(first.get('headline') or '').strip(), str(first.get('details') or '').strip()) if x)
        packet['headline'] = lead[:400]
        packet['details'] = ('Earlier wording, not current where it differs: ' + earlier)[:900]
        packet['corrected'] = True
    if 'why_before' not in p:
        p['why_before'] = p.get('why')
    p['why'] = (lead + (' Earlier wording, not current where it differs: ' + str(p['why_before']) if p.get('why_before') else ''))[:600]
    p['title'] = plain_title if 'what' in changed else plain_title[:108] + ' (corrected)'
    p['corrected'] = {'at': _iso(now), 'by': actor, 'record_only': True}
    new['revision'] = int(idx['revision']) + 1
    if _ref_id(source_ref) or quote or changes.get('quote'):
        new['source_refs'] = (list(new.get('source_refs') or []) + [_ref_entry(source_ref, now, quote or changes.get('quote'))])[-20:]
    _put(con, idx, item, new, now)                                    # a real change to what the item says: its `updated` time moves, so the chat list keeps it in view
    after = _opp(con, key)
    _event(con, idx['commitment_id'], new['revision'], 'corrected', actor, source_ref, op_id, before,
           {'commitment': new, 'item': _item_view(after), 'changed': changed, 'record_only': True, 'correction': text, 'retired_watches': [], 'cancelled_approval': False}, now)
    return {'status': 'corrected', 'commitment': _view(_index(con, idx['commitment_id']), new, after), 'retired_watches': [], 'cancelled_approval': False, 'changed': changed,
            'record_only': True, 'correction': text, 'title': plain_title, 'still_watching': [w.get('id') for w in _item_watches(life, key)]}


# ---- bookkeeping: the next step, what is working it, what proves it -------------------------------------------------------

def _book(con, cid, op: str, op_id, actor, now: datetime, change, *, expected=None, revision=None, source_ref=None, kind: str | None = None) -> dict:
    """One bookkeeping write: the version is checked and stays as it is. change(obj, opp) edits the object in place."""
    _need(op_id, actor)
    now = _now(now)
    ensure_schema(con)
    with _tx(con):
        seen = _op_seen(con, op_id)
        if seen is not None:
            return seen
        idx, opp, obj = _need_open(con, cid)
        if revision is not None or expected is not None or actor != 'owner':
            want = revision if revision is not None else expected
            if want is None:
                raise Refused('This change must say which version of the job it belongs to. Nothing was changed.')
            try:
                same = int(want) == int(idx['revision'])
            except (TypeError, ValueError):
                same = False
            if not same:
                raise Conflict(idx['revision'], _view(idx, obj, opp))
        before = copy.deepcopy(obj)
        change(obj, opp)
        _put(con, idx, opp, obj, now)
        _event(con, idx['commitment_id'], idx['revision'], kind or op, actor, source_ref, op_id, {'commitment': before}, {'commitment': obj}, now)
        result = {'status': 'ok', 'commitment': _view(_index(con, idx['commitment_id']), obj, opp)}
        _op_done(con, op_id, idx['commitment_id'], op, result, now)
    return result


def set_next_action(con, cid, next_action, *, expected_revision, op_id, actor, now=None, source_ref=None) -> dict:
    """Say what happens next for this job, and when. An "arm_monitor" step is checked page by page and makes the job a
    monitor. The version is checked, not raised. -> {'status': 'ok', 'commitment': {...}}"""
    now = _now(now)
    ensure_schema(con)
    na = _next_action(_home(con), next_action, now)

    def change(obj, opp):
        obj['next_action'] = na
        obj.pop('attempts', None)
        if na['do'] == 'arm_monitor':
            obj['kind'] = 'monitor'
    return _book(con, cid, 'next_action', op_id, actor, now, change, expected=expected_revision, source_ref=source_ref)


def record_execution(con, cid, ref, *, revision, op_id, now=None, actor: str = 'worker', source_ref=None) -> dict:
    """Write down what is working this job: {'job': id} | {'watch': id} | {'work_id': id}. Refused (Conflict) when the
    version it was started for is no longer the current one. A watch that is a monitor ({'watch': id, 'monitor':
    True}) makes the job a monitor."""
    if not isinstance(ref, dict) or not any(ref.get(k) for k in ('job', 'watch', 'work_id')):
        raise Refused('It does not say what is working the job: a job, a watch or a work id. Nothing was changed.')

    def change(obj, opp):
        refs = obj.setdefault('execution_refs', {'jobs': [], 'watches': [], 'work_id': None})
        if ref.get('job') and str(ref['job']) not in refs.setdefault('jobs', []):
            refs['jobs'] = (refs['jobs'] + [str(ref['job'])])[-20:]
        if ref.get('watch') and str(ref['watch']) not in refs.setdefault('watches', []):
            refs['watches'].append(str(ref['watch']))
        if ref.get('work_id'):
            refs['work_id'] = str(ref['work_id'])
        if ref.get('watch') and ref.get('monitor'):
            obj['kind'] = 'monitor'
        if obj.get('phase') == 'planned':
            obj['phase'] = 'running'
    return _book(con, cid, 'execution', op_id, actor, now, change, revision=revision, source_ref=source_ref)


def record_verification(con, cid, evidence, *, revision, op_id, now=None, actor: str = 'worker', source_ref=None) -> dict:
    """Write down proof that the job's outcome was checked: {'what': ..., 'ref': ..., 'at': ...}. Refused (Conflict)
    for an older version: proof of the old job is not proof of the corrected one."""
    if not isinstance(evidence, dict) or not _text(evidence.get('what'), 400):
        raise Refused('The proof does not say what was checked ("what"). Nothing was changed.')
    t = _now(now)
    item = {'what': _text(evidence['what'], 400), 'ref': _text(evidence.get('ref'), 200) or None, 'at': _text(evidence.get('at'), 40) or _iso(t)}

    def change(obj, opp):
        obj['evidence_refs'] = (list(obj.get('evidence_refs') or []) + [item])[-20:]
        obj['phase'] = 'verifying'
    return _book(con, cid, 'verification', op_id, actor, now, change, revision=revision, source_ref=source_ref)


# ---- resolve (and lifecli.close's two hooks) -----------------------------------------------------------------------------

def resolve(con, cid, state, why, *, expected_revision=None, op_id, actor, now=None, source_ref=None) -> dict:
    """End the job: 'done' | 'stopped' | 'moot' | 'declined'. The item is closed through lifecli.close, so everything
    that hangs off a closed item (its later checks) ends the way it always did.
    The owner's own word (actor 'owner', no expected_revision) always wins, whatever version the job is at. Anything else must
    say which version it is closing and is refused (Conflict) when the job was corrected since.
    -> {'status': 'resolved' | 'already_closed', 'state': ..., 'commitment': {...}}"""
    _need(op_id, actor)
    if state not in ENDS:
        raise Refused(f'"{_text(state, 40)}" is not a way a job ends. Use one of: {", ".join(ENDS)}. Nothing was changed.')
    now = _now(now)
    ensure_schema(con)
    with _tx(con):
        seen = _op_seen(con, op_id)
        if seen is not None:
            return seen
        idx = _index(con, cid)
        if idx is None:
            raise Refused(f'No job is saved under "{_text(cid, 120)}". Nothing was changed.')
        opp = _opp(con, idx['opp_key'])
        obj = _object(con, idx, opp)
        _check_version(idx, obj, opp, expected_revision, actor)
        if not (_live(opp, obj) or (opp and opp['state'] == HELD)):
            result = {'status': 'already_closed', 'state': opp['state'] if opp else None, 'commitment': _view(idx, obj, opp)}
            _op_done(con, op_id, idx['commitment_id'], 'resolve', result, now)
            return result
        import lifecli as L
        ctx = {'op_id': op_id, 'actor': actor, 'now': now, 'state': state, 'source_ref': source_ref}
        row = {'key': opp['key'], 'state': opp['state'], 'payload': json.dumps(opp['payload'], default=str)}
        by = 'zeus chat' if actor in ('owner', 'chat') else f'commit ({actor})'
        took = L.close(con, row, ENDS[state][0], by, _text(why, 300) or state, None,
                       revision=None if actor == 'owner' else expected_revision, commit=ctx)
        if took is False:
            raise Conflict(idx['revision'], _view(idx, obj, opp))
        if 'error' in ctx:
            raise RuntimeError(ctx['error'])
        return ctx['result']


def may_close(con, key, revision) -> bool:
    """lifecli.close, when a background step closes an item and says which version it is closing (switch
    commit-on): False when the job was corrected since. The owner's own close passes no version and is never asked."""
    if not fence_on(_home(con)):
        return True
    try:
        return fence_ok(con, key, revision)
    except Exception:
        return True


def on_close(con, key, p: dict, state: str, by: str, note: str, ctx=None) -> None:
    """lifecli.close, just before it saves the closed item (switch commit-on, or a resolve() in progress): the item's
    commitment ends with it, in the same save. The version goes up, so a job still running for it can close nothing.
    An item with no commitment closes exactly as it always did. Never raises and never stops a close: trouble is one
    line on the item (commitment_error), and the owner's close still goes through."""
    try:
        if ctx is None and not on(_home(con)):
            return
        ensure_schema(con)
        if _index(con, key) is None:
            return
    except Exception:
        return
    ctx = ctx if ctx is not None else {}
    saved, had = False, copy.deepcopy(p.get('commitment'))
    try:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        con.execute('SAVEPOINT commit_closing')
        saved = True
        now = _now(ctx.get('now'))
        idx = _index(con, key)
        opp = _opp(con, key)
        obj = _object(con, idx, opp) or {}
        phase = ENDS.get(ctx.get('state'), ('', 'done' if state == 'done' else 'stopped'))[1]
        before = copy.deepcopy(obj)
        obj.update(revision=int(idx['revision']) + 1, phase=phase)
        obj.setdefault('kind', idx['kind'])
        p['commitment'] = obj
        con.execute('UPDATE commitments SET revision=?, phase=?, updated=? WHERE commitment_id=?', (obj['revision'], phase, _iso(now), idx['commitment_id']))
        actor = ctx.get('actor') or ('owner' if str(by).startswith('zeus chat') else 'tick')
        op_id = ctx.get('op_id') or _op_for(con, CURRENT_OP, idx['commitment_id'])
        _event(con, idx['commitment_id'], obj['revision'], 'resolved', actor, ctx.get('source_ref'), op_id, {'commitment': before, 'state': opp['state'] if opp else None},
               {'commitment': obj, 'state': state, 'why': _text(note, 300), 'by': str(by)}, now)
        result = {'status': 'resolved', 'state': state,
                  'commitment': _view(_index(con, idx['commitment_id']), obj, (opp or {'key': key, 'surface_at': None, 'payload': p}) | {'state': state, 'payload': p})}
        _op_done(con, op_id, idx['commitment_id'], 'resolve', result, now)
        con.execute('RELEASE commit_closing')
        ctx['result'] = result
    except Exception as e:
        if saved:
            con.execute('ROLLBACK TO commit_closing')
            con.execute('RELEASE commit_closing')
        if had is None:
            p.pop('commitment', None)
        else:
            p['commitment'] = had
        p['commitment_error'] = ctx['error'] = f'{type(e).__name__}: {str(e)[:160]}'


# ---- migration and rollback ----------------------------------------------------------------------------------------------

def migrate(con, now=None) -> list:
    """Every open item (pending, executing, ready, surfaced, needs_attention) that has no commitment gets its first
    version, built only from what the item says. Closed items are not touched; a later check hanging off an item
    ("...#fu1:...") belongs to its parent's commitment and gets none of its own. Titles, results, receipts, the owner's
    recorded yes and every stop stay exactly as they are: only payload['commitment'] is added, and every other column
    of the item, its `updated` time included, is left to the byte.
    -> [{'opp_key', 'commitment_id', 'why'}] for what was changed. A second run changes nothing and returns []."""
    now = _now(now)
    ensure_schema(con)
    out = []
    with _tx(con):
        life = _home(con)
        marks = ','.join('?' * len(OPEN))
        rows = con.execute(f"SELECT key,state FROM opps WHERE state IN ({marks}) AND key NOT LIKE '%#fu%' "
                           "AND key NOT IN (SELECT opp_key FROM commitments) ORDER BY first_seen, key", OPEN).fetchall()
        for r in rows:
            key, state = tuple(r)
            res = _adopt(con, life, key, 'builder', None, None, now)
            out.append({'opp_key': key, 'commitment_id': res['commitment']['commitment_id'], 'why': f'open item ({state}) given its first version'})
        if out or con.execute("SELECT 1 FROM meta WHERE key='commit_migrated_at'").fetchone() is None:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('commit_migrated_at',?)", (_iso(now),))
    return out


def _new_only(idx, obj) -> bool:
    return (obj.get('next_action') or {}).get('do') in CODE_STEPS or idx['kind'] == 'recurring'


def park_new_only(con, now=None) -> list:
    """Before going back to the older engine: a job only this release can carry out (its next step is arm_monitor or
    monitor_running, or it repeats) would look like an ordinary job to the older code, which would work it as one and
    never start its monitor. Each such item is set to the state 'held', which the older code neither lists as open nor
    works; where it stood is kept in payload['commitment']['parked_from']; its watches are stopped ("parked for
    rollback") and listed in parked_watches so they can be brought back. Nothing is deleted.
    -> [{'opp_key', 'commitment_id', 'parked_from', 'watches', 'stopped'}]"""
    now = _now(now)
    ensure_schema(con)
    life = _home(con)
    parked = []
    with _tx(con):
        for idx, opp, obj in _open(con):
            if not _new_only(idx, obj):
                continue
            before = copy.deepcopy(obj)
            watches = list((obj.get('execution_refs') or {}).get('watches') or [])
            obj.update(parked_from=opp['state'], parked_watches=watches, parked_at=_iso(now))
            _put(con, idx, opp, obj, now, state=HELD)
            _event(con, idx['commitment_id'], idx['revision'], 'parked', 'builder', None, None, {'commitment': before, 'state': opp['state']},
                   {'commitment': obj, 'state': HELD}, now)
            parked.append({'opp_key': opp['key'], 'commitment_id': idx['commitment_id'], 'parked_from': opp['state'], 'watches': watches})
    for item in parked:
        item['stopped'] = _stop_watches(life, item['watches'], 'parked for rollback', now)
    return parked


def unpark(con, now=None) -> list:
    """The reverse of park_new_only: each parked item goes back to the state it was in, and its parked watches are
    switched back on (one whose own end time passed while it was parked is not, and is listed under 'lost': the tick's
    reconcile then raises it as a monitor that ended early).
    -> [{'opp_key', 'commitment_id', 'state', 'watches', 'restored', 'lost'}]"""
    now = _now(now)
    ensure_schema(con)
    life = _home(con)
    back = []
    with _tx(con):
        for r in con.execute('SELECT c.commitment_id FROM commitments c JOIN opps o ON o.key=c.opp_key WHERE o.state=?', (HELD,)).fetchall():
            idx = _index(con, tuple(r)[0])
            opp = _opp(con, idx['opp_key'])
            obj = _object(con, idx, opp)
            if not obj or obj.get('parked_from') not in OPEN + ('done',):
                continue
            before = copy.deepcopy(obj)
            state, watches = obj.pop('parked_from'), list(obj.pop('parked_watches', []) or [])
            obj.pop('parked_at', None)
            _put(con, idx, opp, obj, now, state=state)
            _event(con, idx['commitment_id'], idx['revision'], 'unparked', 'builder', None, None, {'commitment': before, 'state': HELD},
                   {'commitment': obj, 'state': state}, now)
            back.append({'opp_key': opp['key'], 'commitment_id': idx['commitment_id'], 'state': state, 'watches': watches})
    for item in back:
        item['restored'] = _restart_watches(life, item['watches'], now)
        item['lost'] = [w for w in item['watches'] if w not in item['restored']]
    return back


def _restart_watches(life, ids, now: datetime) -> list:
    if life is None or not ids:
        return []
    import watches as W

    def again(d):
        done = []
        for w in d['watches']:
            if w.get('id') in ids and w.get('state') == 'stopped' and (w.get('closed') or {}).get('why') == 'parked for rollback':
                end = W._dt(w.get('ends_at'))
                if end is not None and end > now:
                    w.update(state='watching', next_check=W._iso(now))
                    w.pop('closed', None)
                    done.append(w['id'])
        return done
    return W.Store(life).change(again) or []


# ---- the tick: arm, renew, reconcile -------------------------------------------------------------------------------------

def due_actions(con, life, now=None, use=None) -> dict:
    """tick.run_phases' one call, right after the monitors are read and before any model runs (switch commit-on):
      armed       an "arm the monitor" step whose time has come: a watch is added for each page by the same code
                  `life watch add` uses. A page that cannot be read is tried again (three tries, twenty minutes), then
                  the item is set to needs_attention with the plain reason and ONE repair ticket is raised. It is never
                  said to be watching when it is not.
      renewed     a monitor within six hours of its end, whose job is still open, not yet proven done and wanted for
                  longer, is made longer (never more than 48 hours ahead).
      reconciled  the copy of each commitment in its item is the one the history says; a job whose item closed by
                  another road ends with it; a watch a correction took off its job is stopped; a monitor that ended
                  early is raised once.
    `use` stands in for the page readers in tests. Never raises: trouble is listed under 'errors' and the tick goes on."""
    now = _now(now)
    life = Path(life)
    out = {}
    try:
        ensure_schema(con)
    except Exception as e:
        return {'errors': [f'tables: {type(e).__name__}: {str(e)[:160]}']}
    for name, fn in (('armed', lambda: _arm(con, life, now, use)), ('renewed', lambda: _renew(con, life, now)),
                     ('reconciled', lambda: reconcile(con, life, now))):
        try:
            got = fn()
            if got:
                out[name] = got
        except Exception as e:
            if con.in_transaction:
                con.rollback()
            out.setdefault('errors', []).append(f'{name}: {type(e).__name__}: {str(e)[:160]}')
    return out


def _arm(con, life, now: datetime, use) -> dict:
    import monitor as M
    import watches as W
    out = {}
    for idx, opp, obj in _open(con):
        na = obj.get('next_action') or {}
        if na.get('do') != 'arm_monitor' or na.get('gave_up'):
            continue
        legs = [l for l in ((na.get('monitor') or {}).get('legs') or []) if isinstance(l, dict)]
        due = [l for l in legs if not l.get('watch') and not l.get('stopped') and (_moment(l.get('starts')) or _moment(na.get('due')) or now) <= now]
        if not due:
            continue
        if not (W.on(life) and M.on(life)):
            out['switched_off'] = out.get('switched_off', 0) + 1     # watching or monitoring is switched off: nothing is tried, nothing is counted against it
            continue
        tries = [a for a in obj.get('attempts') or [] if a.get('what') == ARM]
        last = _moment(tries[-1].get('at')) if tries else None
        if last is not None and (now - last).total_seconds() < ARM_GAP - 30:
            continue                                                 # tried a moment ago: the page is not read again yet
        armed, failed = {}, []
        for leg in due:
            until = _moment(leg.get('until'))
            end = min(until, now + timedelta(hours=M.MAX_AHEAD_H) - timedelta(minutes=1)) if until else None
            if end is None or end <= now:
                failed.append(f"{leg.get('for')}: its end time has already passed")
                continue
            mon = {k: leg[k] for k in ('every', 'fast_from', 'fast_every', 'trust', 'key', 'done') if leg.get(k) is not None} | {'until': end.isoformat()}
            rec, why = W.add(life, {'for': leg.get('for'), 'read': {'kind': leg.get('kind') or 'web', 'url': leg.get('url')}, 'part': leg.get('part'), 'monitor': mon},
                             by='commit', now_dt=now, use=use, item=opp['key'], item_title=str(opp['payload'].get('title') or ''))
            if rec is None:
                failed.append(f"{leg.get('for')}: {why}")
            else:
                armed[_leg_id(leg)] = rec['id']
        for k, v in _armed(con, life, idx, armed, failed, now).items():
            out[k] = out.get(k, 0) + v
    return out


def _armed(con, life, was, armed: dict, failed: list, now: datetime) -> dict:
    """Write down what one arming run did, in one transaction. If the job was corrected or closed while its pages were
    being read, what was just added is taken down again and nothing is written."""
    res, alarm, stray = {}, None, []
    cid = was['commitment_id']
    with _tx(con):
        idx = _index(con, cid)
        opp = _opp(con, idx['opp_key']) if idx else None
        if idx is None or opp is None or int(idx['revision']) != int(was['revision']) or not _live(opp, _object(con, idx, opp)):
            stray = list(armed.values())
            if stray and idx is not None:
                _event(con, cid, idx['revision'], 'arm_superseded', 'tick', None, None, None, {'retired_watches': stray}, now)
        else:
            obj = _object(con, idx, opp)
            before = copy.deepcopy(obj)
            na = obj['next_action']
            legs = na['monitor']['legs']
            refs = obj.setdefault('execution_refs', {'jobs': [], 'watches': [], 'work_id': None})
            for l in legs:
                wid = armed.get(_leg_id(l))
                if wid:
                    l['watch'] = wid
                    if wid not in refs.setdefault('watches', []):
                        refs['watches'].append(wid)
            state, kind = None, 'armed'
            if armed:
                res['pages_started'] = len(armed)
            if failed:
                kind = 'arm_failed'
                res['pages_failed'] = len(failed)
                obj['attempts'] = (list(obj.get('attempts') or []) + [{'at': _iso(now), 'what': ARM, 'why': '; '.join(failed)[:400]}])[-10:]
                tries = [a for a in obj['attempts'] if a.get('what') == ARM]
                span = (now - (_moment(tries[0].get('at')) or now)).total_seconds()
                if __import__('recover').arm_stops(life, obj, opp, failed, len(tries) >= ARM_TRIES and span >= ARM_SPAN - 30, now):   # recover.py (switch recover-on): the try is marked in the shared history and the policy is asked; it can stop sooner, never later. The same answer without the switch
                    why = (f"Could not start watching ({'; '.join(failed)[:300]}). Tried {len(tries)} times over {round(span / 60)} minutes. "
                           'It is NOT being watched.')
                    na['gave_up'] = {'at': _iso(now), 'why': why}
                    obj['phase'] = 'waiting'
                    opp['payload']['attention'] = {'at': now.timestamp(), 'why': why}
                    state, kind = 'needs_attention', 'arm_gave_up'
                    res['gave_up'] = 1
                    alarm = (opp['key'], str(opp['payload'].get('title') or ''), why)
            if not na.get('gave_up'):
                _settle_next(obj, opp['surface_at'])
                if obj['next_action'].get('do') == 'monitor_running':
                    obj['phase'], obj['kind'] = 'running', 'monitor'
            _put(con, idx, opp, obj, now, state=state)
            _event(con, cid, idx['revision'], kind, 'tick', None, None, {'commitment': before}, {'commitment': obj, 'failed': failed}, now)
    if stray:
        _stop_watches(life, stray, 'superseded by a correction', now)
        return {'superseded': len(stray)}
    if alarm:
        try:
            __import__('worker')._alarm(life, alarm[0], f'Zeus could not start watching: {alarm[1]}', kind=MISSED_KIND, detail=alarm[2])
        except Exception:
            res['alarm_failed'] = 1
    return res


def _wanted_end(obj: dict, wid):
    """Until when this job wants this watch: the end saved with its page, else a fact named "until", else the end
    saved with what would prove the job done."""
    for l in ((obj.get('next_action') or {}).get('monitor') or {}).get('legs') or []:
        if isinstance(l, dict) and l.get('watch') == wid and _moment(l.get('until')):
            wanted = _moment(l['until'])
            break
    else:
        wanted = None
    more = [t for t in ([_moment(f.get('value')) for f in obj.get('facts') or [] if isinstance(f, dict) and f.get('name') == 'until']
                        + [_moment((obj.get('success_condition') or {}).get('until'))]) if t]
    return max([wanted] + more) if wanted and more else (wanted or (max(more) if more else None))


def _renew(con, life, now: datetime) -> dict:
    import monitor as M
    import watches as W
    store = W.Store(life)
    d = store.load()
    if d is None:
        raise RuntimeError('the list of watches could not be read')
    rows = {w.get('id'): w for w in d['watches']}
    n = 0
    for idx, opp, obj in _open(con):
        if obj.get('evidence_refs') or idx['phase'] in ('done', 'stopped'):
            continue                                                 # already proven done, or ended: nothing is made longer
        for wid in (obj.get('execution_refs') or {}).get('watches') or []:
            w = rows.get(wid)
            if not w or w.get('state') not in W.ACTIVE or not isinstance(w.get('monitor'), dict) or w['monitor'].get('terminal'):
                continue
            ends, wanted = W._dt(w.get('ends_at')), _wanted_end(obj, wid)
            if ends is None or wanted is None or (ends - now).total_seconds() > RENEW_WITHIN:
                continue
            to = min(wanted, now + timedelta(hours=M.MAX_AHEAD_H))
            if to <= ends + timedelta(minutes=1):
                continue
            fresh, item = _index(con, idx['commitment_id']), _opp(con, idx['opp_key'])     # still open, still this version, right now
            if not fresh or int(fresh['revision']) != int(idx['revision']) or fresh['phase'] in ('done', 'stopped') or not _live(item, obj):
                continue

            def longer(data, wid=wid, to=to):
                rec = W._find(data, wid)
                if rec is None or rec.get('state') not in W.ACTIVE or not isinstance(rec.get('monitor'), dict):
                    return False
                rec['renewed'] = (list(rec.get('renewed') or []) + [{'at': W._iso(now), 'from': rec.get('ends_at'), 'to': W._iso(to)}])[-5:]
                rec['ends_at'] = rec['monitor']['until'] = W._iso(to)
                return True
            if store.change(longer):
                with _tx(con):
                    _event(con, idx['commitment_id'], idx['revision'], 'renewed', 'tick', None, None, {'watch': wid, 'ends_at': W._iso(ends)},
                           {'watch': wid, 'ends_at': W._iso(to)}, now)
                n += 1
    return {'monitors_made_longer': n} if n else {}


def _retired(con, cid) -> set:
    """Watch ids a correction (or an arming run that was overtaken by one) took off this job."""
    out = set()
    for r in con.execute("SELECT after FROM commit_events WHERE commitment_id=? AND kind IN ('corrected','arm_superseded') AND after IS NOT NULL", (cid,)).fetchall():
        try:
            out.update((json.loads(tuple(r)[0]) or {}).get('retired_watches') or [])
        except (TypeError, ValueError):
            pass
    return out


def _quiet(con, cid, kind: str, now: datetime, change, *, state=None, note=None):
    """One write by the tick on an open commitment, version unchanged. change(obj, opp) -> False to write nothing."""
    with _tx(con):
        idx = _index(con, cid)
        opp = _opp(con, idx['opp_key']) if idx else None
        obj = _object(con, idx, opp) if opp else None
        if idx is None or not _live(opp, obj):
            return False
        before = copy.deepcopy(obj)
        if change(obj, opp) is False:
            return False
        _put(con, idx, opp, obj, now, state=state)
        _event(con, cid, idx['revision'], kind, 'tick', None, None, {'commitment': before}, {'commitment': obj} | (note or {}), now)
    return True


def reconcile(con, life, now=None) -> dict:
    """Every run: what the ledger, the items and the watch list say about each job is made to agree again.
      healed      an item that was written over from an older read gets its commitment back from the history (and, if
                  that read was older than the last correction, the corrected words and times too)
      ended       a job whose item was closed by another road (the worker, the note) ends with it
      stopped     an active watch that a correction took off its job, and that was not stopped then, is stopped now
      listed      an active watch made for an item by a road that did not write it down is written down, not stopped
      dropped     a listed watch that someone stopped is taken off the job
      verified    a listed monitor that ended on a final status is noted as proof
      missed      a listed monitor that ended with no final status while the job is still open and wanted it for
                  longer: ONE repair ticket, and the job waits"""
    import watches as W
    now = _now(now)
    ensure_schema(con)
    out = {}

    def count(k):
        out[k] = out.get(k, 0) + 1

    for idx, opp, obj in _open(con):
        if opp['payload'].get('commitment') != obj:
            with _tx(con):
                opp = _opp(con, idx['opp_key'])
                idx = _index(con, idx['commitment_id'])
                obj = _object(con, idx, opp) if opp else None
                if opp is None or obj is None or not _live(opp, obj) or opp['payload'].get('commitment') == obj:
                    continue
                was, surface = opp['payload'].get('commitment'), None
                if not isinstance(was, dict) or int(was.get('revision') or 0) < int(idx['revision']):
                    r = con.execute("SELECT after FROM commit_events WHERE commitment_id=? AND kind='corrected' ORDER BY seq DESC LIMIT 1", (idx['commitment_id'],)).fetchone()
                    item = (json.loads(tuple(r)[0]) or {}).get('item') if r else None
                    if isinstance(item, dict):
                        surface = _apply_item(opp['payload'], item)
                _put(con, idx, opp, obj, now, surface_at=surface)
                last = con.execute('SELECT kind FROM commit_events WHERE commitment_id=? ORDER BY seq DESC LIMIT 1', (idx['commitment_id'],)).fetchone()
                if last is None or tuple(last)[0] != 'healed':        # something that writes an item over every run must not fill the history with this line
                    _event(con, idx['commitment_id'], idx['revision'], 'healed', 'tick', None, None, {'commitment': was}, None, now)
            count('healed')

    def end_with_items():
        # last, so a job whose last page ended in this very run is ended in this run too
        marks = ','.join('?' * (len(OPEN) + 1))
        for r in con.execute(f"SELECT c.commitment_id FROM commitments c JOIN opps o ON o.key=c.opp_key WHERE c.phase NOT IN ('done','stopped') "
                             f"AND o.state NOT IN ({marks})", OPEN + (HELD,)).fetchall():
            with _tx(con):
                idx = _index(con, tuple(r)[0])
                opp = _opp(con, idx['opp_key'])
                obj = _object(con, idx, opp) or {}
                if opp is None or opp['state'] == HELD or _live(opp, obj):       # told, with its monitor still ahead or running: not ended yet
                    continue
                before = copy.deepcopy(obj)
                obj.update(revision=int(idx['revision']) + 1, phase='done' if opp['state'] == 'done' else 'stopped')
                obj.setdefault('kind', idx['kind'])
                _put(con, idx, opp, obj, now)
                _event(con, idx['commitment_id'], obj['revision'], 'closed_with_item', 'tick', None, None, {'commitment': before}, {'commitment': obj, 'state': opp['state']}, now)
            count('ended')

    d = W.Store(life).load()
    if d is None:
        end_with_items()
        out.setdefault('errors', []).append('the list of watches could not be read; no watch was checked against its job')
        return out
    rows = {w.get('id'): w for w in d['watches'] if isinstance(w, dict)}
    for w in [w for w in rows.values() if w.get('state') in W.ACTIVE and w.get('item')]:
        idx = _index(con, w['item'])
        if idx is None:
            continue
        opp = _opp(con, idx['opp_key'])
        obj = _object(con, idx, opp) or {}
        if w['id'] in ((obj.get('execution_refs') or {}).get('watches') or []) or w['id'] in (obj.get('parked_watches') or []):
            continue
        if w['id'] in _retired(con, idx['commitment_id']):
            if W.stop(life, w['id'], 'superseded by a correction', now):
                count('stopped')
        elif _live(opp, obj):
            def listed(obj, opp, w=w):
                refs = obj.setdefault('execution_refs', {'jobs': [], 'watches': [], 'work_id': None})
                refs.setdefault('watches', []).append(w['id'])
                if w.get('monitor'):
                    obj['kind'] = 'monitor'
            if _quiet(con, idx['commitment_id'], 'execution', now, listed):
                count('listed')

    alarms = []
    for idx, opp, obj in _open(con):
        for wid in list((obj.get('execution_refs') or {}).get('watches') or []):
            w = rows.get(wid)
            if w is not None and w.get('state') in W.ACTIVE:
                continue
            m = w.get('monitor') if w else None
            if w is not None and w.get('state') == 'stopped':
                def dropped(obj, opp, wid=wid):
                    _drop_watch(obj, opp, wid)
                if _quiet(con, idx['commitment_id'], 'execution_dropped', now, dropped, note={'watch': wid, 'why': str((w.get('closed') or {}).get('why') or '')[:200]}):
                    count('dropped')
                continue
            if isinstance(m, dict) and m.get('terminal'):
                if not any(isinstance(e, dict) and e.get('ref') == wid for e in obj.get('evidence_refs') or []):
                    def proven(obj, opp, wid=wid, m=m):
                        obj['evidence_refs'] = (list(obj.get('evidence_refs') or []) + [
                            {'what': f"its page ended on a final status: {m['terminal'].get('word')}", 'ref': wid, 'at': m['terminal'].get('at')}])[-20:]
                        if obj.get('phase') != 'waiting':             # a job that is waiting on a repair stays waiting: one page proven is not the whole job
                            obj['phase'] = 'verifying'
                        _leg_ended(obj, opp, wid)
                    if _quiet(con, idx['commitment_id'], 'verification', now, proven):
                        count('verified')
                continue
            wanted, mark = _wanted_end(obj, wid), f'monitor ended early: {wid}'
            if wanted is None or wanted <= now or any(a.get('what') == mark for a in obj.get('attempts') or []):
                if _has_running_leg(obj, wid) and _quiet(con, idx['commitment_id'], 'execution_ended', now, lambda obj, opp, wid=wid: _leg_ended(obj, opp, wid), note={'watch': wid}):
                    count('finished')                                 # its time ran out (or it was raised already): this page is over, and the next step says so
                continue
            why = (f"Watching \"{(w or {}).get('for') or wid}\" ended {_plain((w.get('closed') or {}).get('at')) if w else 'and its record is gone'} "
                   f"with no final status, but the job wanted it watched until {_plain(wanted)}. It is NOT being watched now.")

            def missed(obj, opp, mark=mark, why=why, wid=wid):
                obj['attempts'] = (list(obj.get('attempts') or []) + [{'at': _iso(now), 'what': mark, 'why': why}])[-10:]
                _leg_ended(obj, opp, wid)
                obj['phase'] = 'waiting'
            if _quiet(con, idx['commitment_id'], 'recovery', now, missed):
                count('missed')
                alarms.append((opp['key'], str(opp['payload'].get('title') or ''), why))
    end_with_items()
    for key, title, why in alarms:
        try:
            __import__('worker')._alarm(life, key, f'Zeus stopped watching before the end: {title}', kind=MISSED_KIND, detail=why)
        except Exception:
            count('alarm_failed')
    return out


def _has_running_leg(obj: dict, wid) -> bool:
    return any(isinstance(l, dict) and l.get('watch') == wid and not l.get('ended') and not l.get('stopped')
               for l in ((obj.get('next_action') or {}).get('monitor') or {}).get('legs') or [])


def _leg_ended(obj: dict, opp: dict, wid) -> None:
    """The watch of one page is over (a final status, its end time, or it was raised as ended early): the page is
    marked so, and the next step follows: more pages still to start or running, or the job's own plain step again."""
    for l in ((obj.get('next_action') or {}).get('monitor') or {}).get('legs') or []:
        if isinstance(l, dict) and l.get('watch') == wid:
            l['ended'] = True
    _settle_next(obj, opp['surface_at'])
    if (obj.get('next_action') or {}).get('do') not in CODE_STEPS and obj.get('phase') == 'running' and opp['state'] != 'executing':
        obj['phase'] = 'planned'


def _drop_watch(obj: dict, opp: dict, wid) -> None:
    refs = obj.setdefault('execution_refs', {'jobs': [], 'watches': [], 'work_id': None})
    refs['watches'] = [x for x in refs.get('watches') or [] if x != wid]
    for l in ((obj.get('next_action') or {}).get('monitor') or {}).get('legs') or []:
        if isinstance(l, dict) and l.get('watch') == wid:
            l['stopped'] = True                                       # the owner stopped this page: it is never started again by itself
    _settle_next(obj, opp['surface_at'])
    if not refs['watches'] and obj.get('phase') == 'running' and opp['state'] != 'executing':
        obj['phase'] = 'planned'


# ---- watches.run: which due read goes first, and saying so when reads fall behind -----------------------------------------

def order_due(con, life, due: list, now: datetime) -> list:
    """watches.run reads at most four pages a run (switch commit-on). When more are due: the one whose job's deadline
    is nearest goes first, then the oldest missed check. A watch with no job is placed by its own end time. Without
    the switch, or on any trouble, the list comes back exactly as it was given."""
    try:
        if con is None or len(due) < 2 or not on(life):
            return due
        import watches as W
        ensure_schema(con)
        far = now + timedelta(days=36500)
        when = {}
        for w in due:
            item = w.get('item')
            if item and item not in when:
                idx, opp = _index(con, item), _opp(con, item)
                p = (opp or {}).get('payload') or {}
                when[item] = (_moment(p.get('deadline')) or _moment(p.get('event_start')) or _moment((opp or {}).get('surface_at'))) if idx else None
        return sorted(due, key=lambda w: (when.get(w.get('item')) or W._dt(w.get('ends_at')) or far, str(w.get('next_check'))))
    except Exception:
        return due


def held_back(store, life, w: dict, now: datetime, why: str) -> None:
    """watches.run, when a due read is left for a later run (the four-reads cap) or its site was read too recently
    (switch commit-on): if that leaves a monitor more than twice its cadence behind, the watch record says so
    (coverage_reduced: since, why), so chat can say so. Cleared by its next good read. Never raises."""
    try:
        if not why or not isinstance(w.get('monitor'), dict) or not on(life):
            return
        import monitor as M
        import watches as W
        last = W._dt(w.get('checked_at'))
        if last is None or (now - last).total_seconds() <= 2 * M.interval(w, now):
            return

        def mark(d):
            rec = W._find(d, w['id'])
            if rec is not None and rec.get('state') in W.ACTIVE and not rec.get('coverage_reduced'):
                rec['coverage_reduced'] = {'since': rec.get('checked_at'), 'why': why}
        store.change(mark)
    except Exception:
        pass


# ---- worker.py's hooks (switch commit-on) --------------------------------------------------------------------------

def fence_home(life) -> None:
    """worker.run says which life folder it is working in, so build_prompt's one added sentence reads the switch there."""
    global _FENCE_HOME
    _FENCE_HOME = Path(life)


def prompt_line(opp: dict) -> str:
    """worker.build_prompt: ONE plain sentence, only when the job was corrected or something about it is not confirmed
    ("Corrected since first asked: arrival is Tue Jan 15 at 8:40PM, not morning. Not confirmed: the traveler's own booking.").
    No field names and no ids. '' without the switch, and for every item with nothing of the kind."""
    try:
        c = opp.get('commitment')
        if not isinstance(c, dict) or _FENCE_HOME is None or not fence_on(_FENCE_HOME):
            return ''
        fixed = [_fixed(f) for f in c.get('facts') or [] if isinstance(f, dict) and ('was' in f or f.get('name') == 'correction')][:3]
        open_ = [_text(u, 200) for u in (c.get('subject') or {}).get('unresolved') or [] if _text(u, 200)][:3]
        if not fixed and not open_:
            return ''
        return ' '.join((['Corrected since first asked: ' + '; '.join(fixed) + '.'] if fixed else []) + (['Not confirmed: ' + '; '.join(open_) + '.'] if open_ else [])) + '\n'
    except Exception:
        return ''


def exec_for(con, life, key, opp: dict, ex: dict, now) -> dict:
    """worker.run, as a job is booked for an item: the job carries the commitment's id and the version its prompt was
    built from, and the commitment lists the job. If the job was corrected while the job was being booked this raises
    Stale: the item is not written, and the next run books a job for the current version. A run that follows a result
    for an older version counts as the item's one retry. Without the switch, or for an item with no commitment, the
    same dict comes back."""
    if not fence_on(life):
        return ex
    c = opp.get('commitment')
    if not isinstance(c, dict) or not c.get('commitment_id'):
        return ex
    out = dict(ex, commitment_id=c['commitment_id'], commitment_rev=int(c.get('revision') or 1))
    if isinstance(opp.get('exec'), dict) and opp['exec'].get('retry_used'):
        out['tries'] = 2
    try:
        got = record_execution(con, c['commitment_id'], {'job': ex.get('job_id')}, revision=out['commitment_rev'], op_id=f"job:{ex.get('job_id')}",
                               now=now, actor='worker')
    except Conflict:
        raise Stale(f'{key}: the job was corrected while its background run was being booked; that run is not used') from None
    except Exception:
        return out                                # the booking stands; the fence still knows which version it was for
    if got:
        opp['commitment'] = _object(con, _index(con, c['commitment_id']), _opp(con, key))     # the worker saves its own copy of the item next: it must carry the job just listed
    return out


def save_guard(con, key, opp: dict) -> tuple:
    """worker._save: the extra condition on its one UPDATE. An item whose job was corrected since this work began is
    not written over: the UPDATE matches nothing, and _save raises Stale. -> ('', ()) without the switch, or when the
    work being saved carries no version."""
    try:
        ex = opp.get('exec') if isinstance(opp.get('exec'), dict) else {}
        if 'commitment_rev' not in ex or not fence_on(_home(con)):
            return '', ()
        ensure_schema(con)
        return ' AND NOT EXISTS (SELECT 1 FROM commitments WHERE opp_key=opps.key AND revision<>?)', (int(ex['commitment_rev']),)
    except Exception:
        return '', ()


def result_current(con, key, opp: dict) -> bool:
    """worker.add_followups: is the result these later checks come from for the current version of the job? True
    without the switch, and for a result no job produced (it was built from the item as it stands)."""
    try:
        ex = opp.get('exec') if isinstance(opp.get('exec'), dict) else {}
        if 'job_id' not in ex or not fence_on(_home(con)):
            return True
        return fence_ok(con, key, ex.get('commitment_rev'))
    except Exception:
        return True


def child_of(con, parent_key, opp: dict, child: dict) -> None:
    """worker.add_followups: a later check carries its parent's commitment id and version. Nothing without the switch."""
    try:
        c = opp.get('commitment')
        if isinstance(c, dict) and c.get('commitment_id') and fence_on(_home(con)):
            child['parent_commitment'] = c['commitment_id']
            child['parent_rev'] = int((opp.get('exec') or {}).get('commitment_rev') or c.get('revision') or 1)
    except Exception:
        pass


def stale_result(con, life, key, ex: dict, packet, now, res: dict, why: str = '') -> bool:
    """worker._harvest_one, when a job's result (or its failure) is in hand: was the job started for an older version?
    -> False: carry on as always. True: the result is written down as a `stale_result` event with its headline and
    nothing else is done with it. The item is left exactly as the correction left it, except that it goes back to
    'pending' so the normal start picks it up for its current version, marked so that run counts as its one retry.
    Later checks scheduled from the stale result are closed."""
    try:
        if not fence_on(life) or 'job_id' not in ex:
            return False
        ensure_schema(con)
        if fence_ok(con, key, ex.get('commitment_rev')):
            return False
        now = _now(now)
        headline = _text((packet or {}).get('headline') or why or 'the run gave no result', 300)
        with _tx(con):
            idx = _index(con, key)
            opp = _opp(con, key)
            if idx is None or opp is None:
                return False
            if opp['state'] == 'executing' and (opp['payload'].get('exec') or {}).get('job_id') == ex.get('job_id'):
                p = opp['payload']
                p['exec'] = {'retry_used': True, 'created_at': ex.get('created_at'),
                             'stale': {'job_id': ex.get('job_id'), 'for_version': ex.get('commitment_rev'), 'at': _iso(now), 'headline': headline}}
                con.execute("UPDATE opps SET state='pending', payload=?, updated=? WHERE key=? AND state='executing'", (json.dumps(p, default=str), now.timestamp(), key))
            _close_children(con, key, 'the result they came from was for an older version of the job', now, keep_version=int(idx['revision']))
            _event(con, idx['commitment_id'], idx['revision'], 'stale_result', 'worker', None, None,
                   {'job_id': ex.get('job_id'), 'for_version': ex.get('commitment_rev')}, {'headline': headline, 'current_version': idx['revision']}, now)
        res['stale'] = res.get('stale', 0) + 1
        return True
    except Exception as e:
        if con.in_transaction:
            con.rollback()
        res.setdefault('errors', []).append(f'stale check {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')
        return False


# ---- lifecli.py / lifewatch.py / timing.py hooks (switch commit-on) ------------------------------------------------------

def take_op(a: list, life) -> list:
    """lifecli.main: `--op <id>` names this request, so a repeat of the same request changes nothing. Only with the
    switch; without it the words come back untouched."""
    global CURRENT_OP
    if not on(life) or '--op' not in a:
        return a
    i = a.index('--op')
    if i + 1 >= len(a):
        return a
    CURRENT_OP = a[i + 1]
    return a[:i] + a[i + 2:]


def repeated(life, db) -> bool:
    """lifecli.main: was this exact request (`--op <id>`) carried out before?"""
    if CURRENT_OP is None or not on(life):
        return False
    try:
        with closing(db()) as con:
            ensure_schema(con)
            return _op_seen(con, CURRENT_OP) is not None
    except Exception:
        return False


def _joined(con, name: str, fn) -> None:
    """Run fn inside the save the caller already has open (its own INSERT or UPDATE), so both land together. If fn
    fails, only fn's writes are undone: the caller's own save still goes through."""
    con.execute(f'SAVEPOINT {name}')
    try:
        fn()
    except Exception:
        con.execute(f'ROLLBACK TO {name}')
        con.execute(f'RELEASE {name}')
        raise
    con.execute(f'RELEASE {name}')


def on_add(con, life, key) -> None:
    """lifecli.add, between its INSERT and its save: the new item gets its commitment (a one-time job, version 1) in
    the same save. Never raises: if this fails the item is saved as before and gets its commitment the first time it
    is touched, or on the next migrate."""
    try:
        if not on(life):
            return
        ensure_schema(con)
        now = _now()

        def go():
            cid_op = CURRENT_OP or uuid.uuid4().hex
            res = _adopt(con, Path(life), key, 'chat', None, cid_op, now, kind='one_time', event='created')
            if _op_seen(con, cid_op) is None:
                _op_done(con, cid_op, res['commitment']['commitment_id'], 'create', res, now)
        _joined(con, 'commit_add', go)
    except Exception:
        pass


def on_move(con, key, was, to, said) -> None:
    """timing.move_cmd, between its UPDATE and its save: the owner moved the item, so the commitment is corrected to the new
    time in the same save (the owner's own word: it always wins). Never raises."""
    try:
        life = _home(con)
        if not on(life):
            return
        ensure_schema(con)
        if _index(con, key) is None:
            return
        now = _now()

        def go():
            idx = _index(con, key)
            op = _op_for(con, CURRENT_OP, idx['commitment_id'])
            res = _correct(con, life, idx['commitment_id'], {'due': to}, None, 'owner', None, op, now, was_due=was, quote=said)
            _op_done(con, op, idx['commitment_id'], 'correct', res, now)
        _joined(con, 'commit_move', go)
    except Exception:
        pass


def on_watches(con, life, key, recs: list, now) -> None:
    """lifewatch._add (--item) and watches.from_job: watches were saved for an item, so its commitment lists them, and
    a monitor makes the job a monitor. An open item from before commitments gets its first version here. Never raises."""
    try:
        if not on(life) or not key or not recs:
            return
        now = _now(now)
        own = con is None
        con = con if con is not None else sqlite3.connect(Path(life) / 'ledger.sqlite3', timeout=15)
        try:
            ensure_schema(con)
            with _tx(con):
                if _index(con, key) is None:
                    opp = _opp(con, key)
                    if opp is None or opp['state'] not in OPEN:
                        return
                    _adopt(con, Path(life), key, 'chat', None, None, now)     # the watch list is read here, so the new watches are already on it
                idx, opp, obj = _need_open(con, key)
                before = copy.deepcopy(obj)
                refs = obj.setdefault('execution_refs', {'jobs': [], 'watches': [], 'work_id': None})
                for rec in recs:
                    if rec.get('id') and rec['id'] not in refs.setdefault('watches', []):
                        refs['watches'].append(rec['id'])
                    if rec.get('monitor'):
                        obj['kind'] = 'monitor'
                if obj.get('phase') == 'planned':
                    obj['phase'] = 'running'
                if obj != before:
                    _put(con, idx, opp, obj, now)
                    _event(con, idx['commitment_id'], idx['revision'], 'execution', 'chat', None, CURRENT_OP, {'commitment': before}, {'commitment': obj}, now)
        finally:
            if own:
                con.close()
    except Exception:
        pass


def on_watch_stop(life, w: dict, now) -> None:
    """lifewatch `life watch stop`: the owner no longer wants this watch, so the job it belonged to no longer lists it, and
    a page of a monitor the owner stopped is never started again by itself. Never raises."""
    try:
        if not on(life) or not w.get('item') or not (Path(life) / 'ledger.sqlite3').exists():
            return
        now = _now(now)
        with closing(sqlite3.connect(Path(life) / 'ledger.sqlite3', timeout=15)) as con:
            ensure_schema(con)
            idx = _index(con, w['item'])
            if idx is not None:
                _quiet(con, idx['commitment_id'], 'execution_dropped', now, lambda obj, opp: _drop_watch(obj, opp, w['id']),
                       note={'watch': w['id'], 'why': 'the owner stopped it'})
    except Exception:
        pass


def starts_hint(life, why: str) -> str:
    """lifewatch._add, after a refusal: when the end time is more than 48 hours away, one sentence with the fix."""
    try:
        import monitor as M
        if on(life) and M.on(Path(life)) and 'hours ahead' in str(why):
            return ' To have it begin later, add --starts <when to begin> to save it to start later.'
    except Exception:
        pass
    return ''


def save_arm_step(life, spec: dict, starts_raw, item_key: str, said: str, now):
    """lifewatch._add with --starts (needs the switches commit-on and watches-on): do not start a monitor now; save an
    "arm the monitor" step on the item's commitment instead, and code starts it at that time (due_actions).
    -> None when the start is not in the future (lifewatch then starts the monitor now, as before), else (exit code,
    the words to print). With no --item a new item is made the way `life add` makes one, due at the start time. A
    second --starts for the same item adds a second page (two at most); the step is due at the earliest start. The
    48 hours a monitor may run are counted from its start."""
    import monitor as M
    import watches as W
    life = Path(life)
    now = _now(now)
    no = '\nNothing is being watched for this. Do not tell the owner you are watching it.'
    if not (on(life) and M.on(life)):
        return 2, 'Not saved: starting a monitor later (--starts) is switched off (commit-on and watches-on are not both in the life folder).' + no
    if not isinstance(spec.get('monitor'), dict):
        return 2, 'Not saved: --starts saves a monitor to begin later, so it needs --every and --until too.' + no
    try:
        starts = _when(starts_raw, now, '--starts')
    except Refused as e:
        return 2, 'Not saved: ' + str(e).replace(' Nothing was saved.', '') + no
    if starts <= now:
        return None
    clean, why = W.validate({k: v for k, v in spec.items() if k != 'monitor'}, life)
    mon, mwhy = M.clean({'monitor': spec['monitor']}, life, starts) if clean is not None else (None, [])
    if clean is None or mon is None:
        return 2, 'Not saved: ' + '; '.join(why + mwhy) + '.' + no
    if not (life / 'ledger.sqlite3').exists():
        return 2, 'Not saved: there is no life ledger yet.' + no
    leg = {'for': clean['for'], 'url': clean['read']['url'], 'kind': clean['read']['kind'], 'part': clean['part'], 'trust': mon['trust'],
           'every': mon['every'], 'until': mon['until'], 'starts': starts.isoformat()}
    leg.update({k: mon[k] for k in ('key', 'fast_from', 'fast_every') if mon.get(k)})
    if spec['monitor'].get('done'):
        leg['done'] = mon['done']
    page = W.read(life, clean['read'])
    status, value = W.extract(clean['part'], page)
    if status == 'ok':
        status, value = M.extract_for({'monitor': mon, 'part': clean['part']}, page, W.extract, now, int(mon['every']) * 60)
    try:
        with closing(sqlite3.connect(life / 'ledger.sqlite3', timeout=15)) as con:
            ensure_schema(con)
            op = CURRENT_OP or uuid.uuid4().hex
            with _tx(con):
                if _op_seen(con, op) is not None:
                    return 0, 'Already done: this request was carried out before. Nothing was changed.'
                if item_key:
                    if _index(con, item_key) is None:
                        _adopt(con, life, item_key, 'chat', None, None, now)
                else:
                    made = _create(con, life, {'what': clean['for'], 'due': starts.isoformat(), 'kind': 'monitor', 'why': said}, 'chat', None, op, now)
                    item_key = made['commitment']['opp_key']
                idx, opp, obj = _need_open(con, item_key)
                before = copy.deepcopy(obj)
                na = obj.get('next_action') or {}
                legs = [l for l in ((na.get('monitor') or {}).get('legs') or []) if isinstance(l, dict)] if na.get('do') in CODE_STEPS else []
                same = next((l for l in legs if _leg_id(l) == _leg_id(leg)), None)
                if same is not None and same.get('watch'):
                    raise Refused('that page is already being watched for this item')
                if same is not None:
                    legs[legs.index(same)] = leg                    # the same page again: its times are replaced
                elif len([l for l in legs if not l.get('stopped')]) >= MAX_LEGS:
                    raise Refused(f'this item already has {MAX_LEGS} pages set to be watched, and that is the most one item takes')
                else:
                    legs.append(leg)
                obj['next_action'] = {'do': 'arm_monitor', 'due': starts.isoformat(), 'depends_on': None, 'monitor': {'legs': legs}}
                obj['kind'] = 'monitor'
                obj.pop('attempts', None)
                _settle_next(obj, opp['surface_at'])
                _put(con, idx, opp, obj, now)
                _event(con, idx['commitment_id'], idx['revision'], 'next_action', 'chat', None, op, {'commitment': before}, {'commitment': obj}, now)
                _op_done(con, op, idx['commitment_id'], 'next_action', {'status': 'ok', 'commitment': _view(idx, obj, opp)}, now)
    except Refused as e:
        return 2, 'Not saved: ' + str(e).replace(' Nothing was saved.', '').replace(' Nothing was changed.', '').rstrip('.') + '.' + no
    except sqlite3.Error as e:
        return 1, f'Not saved: the ledger is busy or could not be written ({type(e).__name__}).' + no
    seen = (f'Right now that page says: "{value}"' if status == 'ok'
            else f'That page could not be read just now ({value}). That is normal when the page for that day does not exist yet: it is tried at the start time, '
                 'and if it still cannot be read then, the item says so and is not watched.')
    return 0, (f"Set to start. Code begins reading it every {mon['every']} min from {_plain(starts)} until {_plain(mon['until'])}. It is not watching yet.\n"
               f'Item: {item_key}.\n{seen}')


# ---- tick.upsert: a later email or calendar entry about a job the owner corrected (switch commit-on) ----------------------------

KEPT_ON_MERGE = ('title', 'why', 'proposed_action', 'steps', 'deadline', 'event_start', 'surface_at', 'done_condition')
SEEN_IN = {'email': 'An email', 'calendar': 'The calendar', 'watched page': 'A watched page'}


def corrected(payload) -> bool:
    """Has the owner had a hand in this job: was it corrected (its version is above 1), or does it carry a fact that came in
    with the owner's own verified words?"""
    c = payload.get('commitment') if isinstance(payload, dict) else None
    if not isinstance(c, dict):
        return False
    try:
        if int(c.get('revision') or 1) > 1:
            return True
    except (TypeError, ValueError):
        pass
    return any(isinstance(f, dict) and f.get('by') == 'owner' for f in c.get('facts') or [])


def merge_kept(life, old: dict, merged: dict, obs: dict, opp: dict) -> dict:
    """tick.upsert, as a new email or calendar entry is folded into an open item. Left alone, the noticer's new reading
    would replace the item's title, reason, steps and times, so an entry that still carried the old time would put stale
    words back into a job the owner had corrected. For a corrected job those stay as they are; what the new entry says is added to
    the evidence (and merge_noted writes it on the commitment). Any other item, and every item without the switch,
    gets the same dict back."""
    try:
        if not on(life) or not corrected(old):
            return merged
        out = dict(merged)
        for k in KEPT_ON_MERGE:
            if k in old:
                out[k] = old[k]
            else:
                out.pop(k, None)
        had, seen = str(old.get('evidence') or ''), _text(opp.get('evidence') or opp.get('title'), 300)
        out['evidence'] = had if not seen or seen in had else (had + f" | Also seen ({SEEN_IN.get(obs.get('source'), 'a new observation').lower()}): {seen}")[:1200]
        return out
    except Exception:
        return merged


def merge_move(life, before: dict, move: dict, opp: dict) -> dict:
    """tick._merge_timed, after timing.classify said where the due time should go. For a job the owner corrected, a time the
    new entry carries does not move it (the same event "moved", or the entry asks for an earlier time): the owner's time
    stands. Only an urgent update still brings the work forward to now, because that says "look at this now", not
    "the time is different". Without the switch, or for any other item, the move comes back as it was."""
    try:
        if move.get('to') and on(life) and corrected(before) and not (move.get('kind') == 'urgent' and opp.get('urgency') == 'now'):
            return dict(move, to=None, why=str(move.get('why') or '') + '; not applied: the owner corrected this job and its time stands')
    except Exception:
        pass
    return move


def _clashes(con, obj: dict, item: dict, obs: dict, opp: dict) -> list:
    """Does the new entry repeat something the owner corrected away? -> plain lines for subject.unresolved. Only what code can
    check: a time the entry carries that equals a time from before a correction, and an old value of a corrected fact
    that the entry's words still carry while they do not carry the new one."""
    src = SEEN_IN.get(obs.get('source'), 'A new observation')
    seen = [t for t in [_moment(opp.get(k)) for k in ('event_start', 'surface_at', 'deadline')] + [_moment(obs.get('start'))] if t]
    words = _flat(' '.join(str(x or '') for x in (opp.get('title'), opp.get('evidence'), opp.get('why'), obs.get('title'), obs.get('subject'))))
    same = lambda a, b: abs((a - b).total_seconds()) < 60
    out = []
    for f in obj.get('facts') or []:
        if not isinstance(f, dict) or 'was' not in f or f.get('name') == 'correction':
            continue
        was = _moment(f['was'])
        if was is not None and any(same(t, was) for t in seen):
            out.append(f"{src} still says {_plain(was)}. The owner's correction stands: {_fact_name(f)} is {_fact_value(f.get('value'))}.")
        elif was is None and len(_flat(f['was'])) >= 4 and _flat(f['was']) in words and _flat(f.get('value')) not in words:
            out.append(f'{src} still says "{_text(f["was"], 80)}". Their correction stands: {_fact_name(f)} is {_fact_value(f.get("value"))}.')
    now_at = _moment(item.get('surface_at'))
    for r in con.execute("SELECT before FROM commit_events WHERE commitment_id=? AND kind='corrected' AND before IS NOT NULL ORDER BY seq", (obj.get('commitment_id'),)).fetchall():
        try:
            was = _moment(((json.loads(tuple(r)[0]) or {}).get('item') or {}).get('surface_at'))
        except (TypeError, ValueError):
            was = None
        if was is not None and now_at is not None and not same(was, now_at) and any(same(t, was) for t in seen):
            line = f'{src} still says {_plain(was)}. Their corrected time stands: {_plain(now_at)}.'
            if line not in out:
                out.append(line)
    return out[:3]


def merge_noted(con, life, key, obs: dict, opp: dict) -> None:
    """tick.upsert, right after a new entry was folded into an item whose job the owner corrected: what the entry says is
    written on the commitment as a fact with where it came from (not certain, and never over a fact of the owner's), and when
    it repeats something the owner corrected away, that clash is written into subject.unresolved in plain words, so the
    worker and chat both see it. The version does not change. Never raises."""
    try:
        if not on(life):
            return
        ensure_schema(con)
        idx = _index(con, key)
        item = _opp(con, key) if idx else None
        if not item or not corrected(item['payload']):
            return
        now = _now()
        name = 'seen in ' + SEEN_IN.get(obs.get('source'), 'a new observation').split(' ', 1)[1].lower()
        value = _text(': '.join(str(x) for x in (opp.get('title'), opp.get('evidence')) if x), 300)

        def change(obj, row):
            subj = obj.setdefault('subject', {'what': '', 'identifiers': {}, 'unresolved': []})
            facts = obj.setdefault('facts', [])
            fact = {'name': name, 'value': value, 'source_ref': _text(obs.get('id'), 200), 'certain': False, 'at': _iso(now), 'by': 'tick'}
            had = next((f for f in facts if isinstance(f, dict) and f.get('name') == name), None)
            fresh = had is None or had.get('value') != value or had.get('source_ref') != fact['source_ref']
            if had is None:
                facts.append(fact)
            elif fresh:
                had.clear()
                had.update(fact)
            lines = [l for l in _clashes(con, obj, row, obs, opp) if l not in (subj.get('unresolved') or [])]
            if not fresh and not lines:
                return False
            subj['unresolved'] = (list(subj.get('unresolved') or []) + lines)[-10:]
        _quiet(con, idx['commitment_id'], 'observed', now, change, note={'source': _text(obs.get('id'), 200)})
    except Exception:
        if con.in_transaction:
            con.rollback()


# ---- `life correct`: the owner corrected something about an item (switch commit-on) --------------------------------------------

def _reason(e) -> str:
    """A Refused message as the tail of "Not corrected: ...": without the closing "Nothing was changed.", no full stop."""
    text = re.sub(r'\s*Nothing was (?:changed|saved)\.?', '', str(e)).strip().rstrip('.')
    return (text[:1].lower() + text[1:]) if text[:1].isupper() and not text[1:2].isupper() else text


def correct_cmd(a: list, life, db, find, owners_message) -> int:
    """life correct <item> ["<the corrected thing in plain words>"] [--due <ISO | +2h | +3d>] [--was <the time it replaces>] [--deadline <ISO>]
                 [--title "<new title>"] [--fact <name>=<value>]... [--unsure "<what is not certain>"] --said "<the owner's exact words>"
    The owner corrected something about an item. This is commit.correct for the chat Zeus:
      --said    the owner's exact words. When they are in a real message of the owner's from the last 48 hours (the check `life grant`
                uses) the correction is the owner's own and always goes through; otherwise it is recorded as Zeus's note, and
                the answer says so.
      a sentence with no flag: saved as the fact "correction"; the item's reason becomes it and every step carries it.
      --due     the new time. A time with no offset is not certain enough and is refused, unless --unsure says what is
                not certain: then the earlier reading is saved (morning, for an hour before 12) with the doubt beside it.
                A monitor that is set to start later moves with it, by the same amount, when the item's own time was the
                event's time; say --was <the time it replaces> (8:40am, now 8:40pm) when it was not, and it moves by
                that difference. If neither tells how far, the monitor stays and the item says its times were not moved.
    Answers, first word first: "Corrected. <title>, due <when>." then, when they apply, "Tracking that no longer
    matches was stopped: <n>." and "The earlier yes no longer applies; ask again."; "Not corrected: it changed while
    you were working. Read it again and retry." (exit 3); "Not corrected: <plain reason>." (exit 2).
    An item that is already finished (done, dropped, no longer needed): only the record is put right (_correct_record)
    and the answer is: Corrected. <title> (<the correction>). It was already finished, so nothing new is scheduled."""
    opts, facts, words, i = {}, [], [], 0
    while i < len(a):
        if a[i] in ('--due', '--was', '--deadline', '--title', '--fact', '--unsure', '--said') and i + 1 < len(a):
            if a[i] == '--fact':
                facts.append(a[i + 1])
            else:
                opts[a[i][2:]] = a[i + 1]
            i += 2
        else:
            words.append(a[i])
            i += 1

    def no(why, code=2):
        print('Not corrected: ' + str(why).strip().rstrip('.') + '.')
        return code

    if not words:
        return no('it does not say which item (life correct <item> ... --said "<their exact words>")')
    said, sentence, unsure = _text(opts.get('said'), 600), ' '.join(words[1:]).strip(), _text(opts.get('unsure'), 300)
    if len(said) < 3:
        return no('their exact words are needed: --said "<their exact words>"')
    if opts.get('was') and not opts.get('due'):
        return no('the time it replaces (--was) only goes with the new time (--due)')
    if not (sentence or facts or unsure or any(opts.get(k) for k in ('due', 'deadline', 'title'))):
        return no('it does not say what is corrected: a sentence in plain words, or --due, --title, --deadline, --fact name=value')
    now = _now()
    changes = {'quote': said}
    if opts.get('title'):
        changes['what'] = opts['title']
    if sentence:
        changes['correction'] = sentence
    if opts.get('deadline'):
        changes['deadline'] = opts['deadline']
    if opts.get('was'):
        changes['was'] = opts['was']
    if opts.get('due'):
        raw = str(opts['due']).strip()
        try:
            changes['due'] = _when(raw, now, 'the time').isoformat()
            if unsure:
                changes['due_unsure'] = {'why': unsure, 'use_given': True}
        except Refused as e:
            try:
                bare = None if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw) else datetime.fromisoformat(raw)
            except ValueError:
                bare = None
            if bare is None or bare.tzinfo is not None:
                return no(_reason(e))
            if not unsure:
                return no(f'the time "{raw}" has no offset, so it is not certain which moment it is (morning or evening, or on which clock). Give the full time with '
                          'its offset, like 2030-01-15T20:40:00-05:00; if that is not known, say what is not certain with --unsure "<what is not certain>"')
            local = bare.replace(tzinfo=_tz())
            changes['due'] = local.isoformat()
            changes['due_unsure'] = ({'why': unsure, 'other': (local + timedelta(hours=12)).isoformat(), 'use_earlier': True} if bare.hour < 12
                                     else {'why': unsure, 'use_given': True})
    elif unsure:
        changes['unsure'] = unsure
    try:
        with closing(db()) as con:
            ensure_schema(con)
            r, _near = find(con, words[0])
            key = None if r is None else (r['key'] if hasattr(r, 'keys') else tuple(r)[0])
            if key is not None:
                # lifecli.find takes the best match on ANY one word ("the" is enough). A correction rewrites an item, so here
                # the words must all be the item's own: its key, the start of its key, or every word in its key or title.
                named, hay = _text(words[0], 300).lower(), (key + ' ' + str((_opp(con, key) or {}).get('payload', {}).get('title') or '')).lower()
                if not (key.lower() == named or key.lower().startswith(named) or all(w in hay for w in named.replace(':', ' ').split() if len(w) > 2)):
                    key = None
            if key is None:
                return no(f'no single item matches "{_text(words[0], 80)}"')
            have = get(con, key)
            try:
                found = owners_message(said)
            except Exception:
                found = None
            actor = 'owner' if found is not None else 'chat'
            for raw in facts:
                name, eq, value = str(raw).partition('=')
                if not eq or not name.strip() or not value.strip():
                    return no(f'a fact is written name=value ("{_text(raw, 60)}" is not)')
                changes.setdefault('facts', []).append({'name': name.strip(), 'value': value.strip(), 'certain': actor == 'owner'})
                if have and name.strip() in ((have.get('subject') or {}).get('identifiers') or {}):       # the job is known by this name (its flight, its order): that changes with it
                    changes.setdefault('subject', {}).setdefault('identifiers', {})[name.strip()] = value.strip()
            res = correct(con, life, key, changes, expected_revision=None if actor == 'owner' else (have['revision'] if have else 1),
                          op_id=CURRENT_OP or uuid.uuid4().hex, actor=actor, source_ref={'kind': 'chat', 'id': str(found.get('id') or '')} if found else None, now=now)
    except Conflict:
        print('Not corrected: it changed while you were working. Read it again and retry.')
        return 3
    except Refused as e:
        return no(_reason(e))
    except sqlite3.Error as e:
        return no(f'the ledger is busy or could not be written ({type(e).__name__}), so nothing was changed; try again', 1)
    c = res['commitment']
    if res.get('record_only'):          # it was already finished: the record was put right, and that is all that happened
        print(f"Corrected. {_text(res.get('title'), 200).rstrip('.')} ({res.get('correction')}). It was already finished, so nothing new is scheduled."
              + (' A watch for it is still running and was left as it is.' if res.get('still_watching') else ''))
        if actor == 'chat':
            print('(Those words were not found in a message of theirs from the last two days, so they are kept as your note, not as their words.)')
        return 0
    doubt = next((u for u in reversed((c.get('subject') or {}).get('unresolved') or []) if str(u).startswith('Time not confirmed:')), '') if 'due_unsure' in changes else ''
    print(f"Corrected. {_text(c.get('title'), 200).rstrip('.')}" + (f" ({sentence.rstrip('.')})" if sentence else '') + f", due {_plain(c.get('surface_at'))}."
          + (' ' + doubt if doubt else ''))
    stopped = len(res.get('retired_watches') or []) + len(res.get('retired_legs') or [])
    if stopped:
        print(f'Tracking that no longer matches was stopped: {stopped}.')
    if res.get('cancelled_approval'):
        print('The earlier yes no longer applies; ask again.')
    if actor == 'chat':
        print('(Those words were not found in a message of theirs from the last two days, so they are kept as your note, not as their words.)')
    return 0


# ---- the health note -----------------------------------------------------------------------------------------------------

def health_part(life, note: dict) -> None:
    """tick.health_note's call. Only with the switch commit-on: a small `commitments` part (how many jobs are open, how
    many have no next step, how many "arm the monitor" steps are waiting and how many of those are overdue, when the
    last migrate ran). Without the switch the note is exactly what it was. Never raises."""
    try:
        life = Path(life)
        if not on(life) or not (life / 'ledger.sqlite3').exists():
            return
        now = _now()
        with closing(sqlite3.connect(f"file:{life / 'ledger.sqlite3'}?mode=ro", uri=True, timeout=5)) as con:
            try:
                rows = _open(con)
                last = con.execute("SELECT value FROM meta WHERE key='commit_migrated_at'").fetchone()
            except sqlite3.OperationalError:
                note['commitments'] = {'readable': False}
                return
        arm = [o for _i, _p, o in rows if (o.get('next_action') or {}).get('do') == 'arm_monitor' and not (o.get('next_action') or {}).get('gave_up')]
        note['commitments'] = {'readable': True, 'open': len(rows),
                               'no_next_action': sum(1 for _i, _p, o in rows if not (o.get('next_action') or {}).get('do')),
                               'arm_pending': len(arm),
                               'arm_overdue': sum(1 for o in arm if (_moment(o['next_action'].get('due')) or now) < now - timedelta(minutes=30)),
                               'arm_gave_up': sum(1 for _i, _p, o in rows if (o.get('next_action') or {}).get('gave_up')),
                               'last_migrate': tuple(last)[0] if last else None}
    except Exception as e:
        note['commit_health_error'] = f'{type(e).__name__}: {str(e)[:120]}'


# ---- `life commit <verb>`: one JSON object in, one JSON object out --------------------------------------------------------

def cli(a: list, life, db) -> int:
    """`life commit <create|correct|next|exec|verify|resolve|explain|coverage|touched>`: reads ONE JSON object on
    standard input and prints ONE JSON object. Errors are JSON too: {"error": "...", "current": {...}} with exit code 3
    for a version clash, 2 for a bad request, 1 when the ledger is missing or busy. Nothing is changed on any error.
    Request keys: id (a commitment id or an item key), op_id (made up here when left out, and handed back), actor
    (chat unless said), source_ref (the chat turn id), expected_revision; then per verb:
      create    the job itself: what, due, kind, why, subject, success_condition, facts, authority_ref, next_action ...
      correct   changes {...}
      next      next_action {...}
      exec      ref {...}, revision
      verify    evidence {...}, revision
      resolve   state, why
      explain, coverage   id
      touched   source_ref"""
    def out(obj, code=0):
        print(json.dumps(obj, ensure_ascii=False, default=str))
        return code

    verb = a[0] if a else ''
    if verb not in VERBS:
        return out({'error': f'"commit {verb}" is not a command. Use one of: {", ".join(VERBS)}. Nothing was changed.'}, 2)
    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        return out({'error': f'The request is not valid JSON ({str(e)[:80]}). Nothing was changed.'}, 2)
    if not isinstance(req, dict):
        return out({'error': 'The request must be one JSON object. Nothing was changed.'}, 2)
    if not (Path(life) / 'ledger.sqlite3').exists():
        return out({'error': 'There is no life ledger yet, so nothing was saved.'}, 1)
    op = _text(req.get('op_id'), 200) or CURRENT_OP or uuid.uuid4().hex
    actor, ref, cid = req.get('actor') or 'chat', req.get('source_ref'), req.get('id') or req.get('commitment_id') or req.get('opp_key')
    now = _now()
    try:
        with closing(db()) as con:
            ensure_schema(con)
            if verb == 'create':
                spec = req['spec'] if isinstance(req.get('spec'), dict) else {k: v for k, v in req.items() if k not in ('op_id', 'actor', 'source_ref', 'id', 'commitment_id', 'expected_revision')}
                res = create(con, life, spec, op_id=op, actor=actor, source_ref=ref, now=now)
            elif verb == 'correct':
                res = correct(con, life, _wanted(cid), req.get('changes'), expected_revision=req.get('expected_revision'), op_id=op, actor=actor, source_ref=ref, now=now)
            elif verb == 'next':
                res = set_next_action(con, _wanted(cid), req.get('next_action'), expected_revision=req.get('expected_revision'), op_id=op, actor=actor, now=now, source_ref=ref)
            elif verb == 'exec':
                res = record_execution(con, _wanted(cid), req.get('ref'), revision=req.get('revision', req.get('expected_revision')), op_id=op, now=now, actor=actor, source_ref=ref)
            elif verb == 'verify':
                res = record_verification(con, _wanted(cid), req.get('evidence'), revision=req.get('revision', req.get('expected_revision')), op_id=op, now=now, actor=actor, source_ref=ref)
            elif verb == 'resolve':
                res = resolve(con, _wanted(cid), req.get('state'), req.get('why'), expected_revision=req.get('expected_revision'), op_id=op, actor=actor, now=now, source_ref=ref)
            elif verb in ('explain', 'coverage'):
                cid = _wanted(cid)
                item = _opp(con, cid) if _index(con, cid) is None else None
                if item is not None and item['state'] in OPEN:        # an open item from before commitments: its first version, then the answer
                    adopt(con, cid, op_id=uuid.uuid4().hex, actor=actor, now=now, source_ref=ref)
                return out(explain(con, life, cid) if verb == 'explain' else coverage(con, life, cid))
            else:
                return out({'source_ref': _ref_id(ref), 'commitments': touched_by(con, ref)})
    except Conflict as e:
        return out({'error': str(e), 'current': e.current}, 3)
    except Refused as e:
        return out({'error': str(e)}, 2)
    except sqlite3.Error as e:
        return out({'error': f'The ledger is busy or could not be read ({type(e).__name__}: {str(e)[:80]}). Nothing was changed; try again.'}, 1)
    except Exception as e:
        return out({'error': f'That could not be finished ({type(e).__name__}: {str(e)[:120]}). Read the job again (commit explain) before saying anything about it.'}, 1)
    return out(dict(res) | {'op_id': op})


def _wanted(cid):
    if not _text(cid, 300):
        raise Refused('The request does not say which job ("id": a commitment id or an item key). Nothing was changed.')
    return _text(cid, 300)
