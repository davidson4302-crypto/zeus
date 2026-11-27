"""recover: ONE recovery policy, as data, and ONE attempt history.

Why: every place that can fail would otherwise decide alone what to do next. The worker has its two runs, the arm
step its three tries, a watch its backoff, an unknown send its read-back, a lapsed sign-in its hold. Kept apart, each
count lives in its own place, none knows what the others tried, and "what now?" after the last try differs in each.

What this file is:
  POLICY      the plan's table, one row per kind of failure: what is done about it, whether another try is allowed,
              whether a repair ticket is raised, whether the owner is asked. Data, read by next_step.
  note        one line in the item's attempt history. The history is the list the arm step already keeps
              (payload['commitment']['attempts']; an item without a commitment gets payload['attempts']), so the worker,
              the monitors, the arm step and the repair step all write to, and read from, the same list.
  next_step   the policy's answer for one failure of one item, given that history.
  escalation  when recovery is out of road: plain FACTS for the note writer (what is unfinished, what was tried, the
              exact thing the owner must provide or decide, what happens straight afterwards). Never a sentence glued on to a
              model's text: the writer says it, code checks the ask is still in the note (ask_in).

Rules this file keeps:
- Recovery never multiplies attempts. Every ceiling stays where it is (worker.py: the first run plus one more;
  commit.py: three tries to start a monitor; watches.py: the site floor). next_step can only say "no more" sooner than
  the old code would, never "one more" after it. A hook that hands back a yes/no hands back the caller's own answer
  or a stricter one.
- An uncertain send, write or purchase is settled by reading where it would have gone. next_step never answers
  "try again" for it, whatever the history says.
- After the last try: a deadline within two hours means the concrete problem goes to the owner at the next moment the
  quiet-hour code allows (this file never looks at the clock for that: it makes an ordinary ready item and
  notifier.py decides). A later or unknown deadline keeps the repair ticket and puts ONE useful request in the next
  morning note. The same ask is never made twice: an ask is the same ask while the commitment id, its version and the
  failure are the same, and that it was asked is written on the item.
- Resume: when a sign-in comes back, or the owner supplies the missing fact, the SAME item goes back to work, once.
- A source that could not be read is "could not be read". Nothing here turns a failure into "nothing there", and
  nothing prepared is called done.

Switch file in the life folder: `recover-on`. Without it every hook below hands back exactly what it was given and
writes nothing. No new table: what recovery knows about an item lives in the item (payload['recovery']) and in the
attempt history.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import settings

SWITCH = 'recover-on'
NEAR = 2 * 3600                 # a deadline this close: the problem goes to the owner at the next allowed moment
WORKER_RUNS = 2                 # = worker.py's own ceiling (the `tries < 2` check): the first run plus one recovery run
OWN_LINES = 5                   # this file's lines in the shared history. The arm step keeps the last ten lines of the same list and counts its own three among them: 3 + 2 marks + 5 never pushes one of its lines out
SETTLE_READS = 3                # an unknown email read back this many times and still unknown: the owner is asked to look
BY = 'recover'                  # the mark on a line this file wrote (the arm step's own lines carry none)
SEARCHED = 'search of the permitted sources'
PERMITTED = ('their email', 'their calendars', 'their contacts, reminders and notes', 'your own memory', 'the public web')
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')

# The plan's table (handoff section 4.6). `handling` is the row's own words. `do` is what a caller acts on.
POLICY = {
    'temporary_read_failure': {'handling': 'retry once at the next eligible tick, respecting the per-site floor',
                               'do': 'retry_read', 'retry': True, 'ticket': False, 'ask': False},
    'repeated_read_failure': {'handling': 'one available alternative source that is as authoritative',
                              'do': 'alternative_source', 'retry': False, 'ticket': False, 'ask': False},
    'both_routes_unavailable': {'handling': 'stay waiting; one recovery ticket',
                                'do': 'wait', 'retry': False, 'ticket': True, 'ask': False},
    'signin_expired': {'handling': 'the existing sign-in helper (it never types a texted code)',
                       'do': 'signin_helper', 'retry': False, 'ticket': False, 'ask': False},
    'access_needed': {'handling': 'ONE prepared access request; resume when access is verified',
                      'do': 'access_request', 'retry': False, 'ticket': False, 'ask': True},
    'job_never_started': {'handling': 'read the execution records before requeueing',
                          'do': 'requeue_after_records', 'retry': True, 'ticket': False, 'ask': False},
    'job_interrupted': {'handling': 'inspect what it already did, then resume only the safe remaining work',
                        'do': 'resume_safe_work', 'retry': True, 'ticket': False, 'ask': False},
    'uncertain_effect': {'handling': 'settle by reading the destination; never a blind retry',
                         'do': 'settle_by_reading', 'retry': False, 'ticket': False, 'ask': False},
    'missing_fact': {'handling': 'search the permitted sources before asking the owner',
                     'do': 'search_first', 'retry': True, 'ticket': False, 'ask': True},
    'budget_exhausted': {'handling': 'stay pending; look again when the budget resets or the deadline nears',
                         'do': 'stay_pending', 'retry': False, 'ticket': False, 'ask': False},
    'stopped': {'handling': 'stop the affected work and cancel its future actions',
                'do': 'stop', 'retry': False, 'ticket': False, 'ask': False},
}
FAILURES = tuple(POLICY)

# Plain words for the note writer. First person, talking to the owner: the writer is Zeus and these are its own facts.
NEEDED = {
    'access_needed': 'Finish the {name} sign-in yourself, once, in my own Chrome: {why}.',
    'signin_expired': 'Sign in to {name} once in my own Chrome.',
    'uncertain_effect': 'Look at where it would have gone ({where}) and tell me whether it is there.',
}
THEN = {
    'access_needed': 'I see the sign-in by myself and pick this up where I left off.',
    'signin_expired': 'I see the sign-in by myself and pick this up where I left off.',
    'uncertain_effect': 'If it is there I close this. If it is not, I get it ready again for your yes. I do not do it a second time on a guess.',
    'missing_fact': 'I carry on with it straight away, once.',
    'both_routes_unavailable': 'I keep trying on my usual schedule and it is flagged for repair. Until a read works, nobody is watching it.',
    'job_interrupted': 'Nothing more happens by itself. It is flagged for repair.',
    'job_never_started': 'Nothing more happens by itself. It is flagged for repair.',
    'repeated_read_failure': 'I keep trying on my usual schedule and it is flagged for repair.',
}
WHAT = {'worker': 'background run', 'arm': 'start of the watch', 'send': 'read of the thread to see whether the reply went',
        'signin': 'sign-in', 'repair': 'repair attempt', 'watch': 'read of the watched page', 'monitor': 'read of the watched page'}
ACCESS_STOPS = {'code_step': 'the store asks for a code sent to your phone or email, and I never type those',
                'no_login_saved': 'I have no saved login for it',
                'robot_check': 'the store shows a robot check that only a person can pass',
                'locked_notice': 'the store shows a locked or unusual-activity notice',
                'account_picker': 'the store asks which account to use',
                'password_not_accepted': 'the store did not take the saved password'}


# ---- small things --------------------------------------------------------------------------------------------------------

def on(life) -> bool:
    try:
        return life is not None   # always on: the recover-on switch is folded in
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
    """A moment, with its zone. A bare number is seconds; nothing is now."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_tz())
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), _tz())
    if value:
        try:
            t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return t if t.tzinfo else t.replace(tzinfo=_tz())
        except ValueError:
            pass
    return datetime.now(_tz())


def _moment(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00')) if value else None
    except ValueError:
        return None
    return None if t is None else (t if t.tzinfo else t.replace(tzinfo=_tz()))


def _iso(dt: datetime) -> str:
    return dt.astimezone(_tz()).isoformat(timespec='seconds')


def _one(text, limit: int = 300) -> str:
    return ' '.join(str(text or '').split())[:limit]


def _clock(value, now) -> str:
    """'today at 2:06pm' / 'Sat Nov 14 at 2:06pm'. For facts a person may read. now=None: always with its date, for
    words that are stored and read on a later day ("today" would be false by then)."""
    t = _moment(value)
    if t is None:
        return 'an unknown time'
    t = t.astimezone(_tz())
    hour = t.strftime('%I:%M%p').lstrip('0').lower()
    return ('today at ' if now is not None and t.date() == now.astimezone(_tz()).date() else t.strftime('%a %b %d at ').replace(' 0', ' ')) + hour


def _times(n: int) -> str:
    return {1: 'once', 2: 'twice'}.get(int(n), f'{int(n)} times')


def _home(con):
    try:
        for row in con.execute('PRAGMA database_list'):
            row = tuple(row)
            if row[1] == 'main' and row[2]:
                return Path(row[2]).parent
    except Exception:
        return None
    return None


def _item(con, key):
    """-> (state, payload, surface_at, urgency) or None."""
    r = con.execute('SELECT state,payload,surface_at,urgency FROM opps WHERE key=?', (str(key),)).fetchone()
    if r is None:
        return None
    r = tuple(r)
    try:
        p = json.loads(r[1])
    except (TypeError, ValueError):
        p = {}
    return r[0], (p if isinstance(p, dict) else {}), r[2], r[3]


def _set(con, key, name: str, value, opp=None) -> None:
    """One field of the item, written by itself (never the whole item from an older read: the tick, the worker and the owner's
    chat all write items). The caller's own copy is kept in step when it hands one over."""
    con.execute("UPDATE opps SET payload=json_set(payload, ?, json(?)), updated=? WHERE key=?",
                ('$.' + name, json.dumps(value, ensure_ascii=False, default=str), time.time(), str(key)))
    if isinstance(opp, dict):
        opp[name] = value


def _state(p: dict) -> dict:
    s = p.get('recovery')
    return dict(s) if isinstance(s, dict) else {}


def deadline(p: dict):
    """The earliest time the item itself gives for when something lands (its deadline, the start of its event)."""
    times = [t for t in (_moment(p.get('deadline')), _moment(p.get('event_start'))) if t]
    return min(times) if times else None


def near(p: dict, now: datetime) -> bool:
    d = deadline(p)
    return d is not None and (d - now).total_seconds() <= NEAR


def identity(p: dict, key: str, failure: str) -> str:
    """What makes an ask the same ask: the commitment id (the item's key when it has none), its version, the failure."""
    c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
    return f"{c.get('commitment_id') or key}|{int(c.get('revision') or 0)}|{failure}"


# ---- the one attempt history ---------------------------------------------------------------------------------------------

def history(opp: dict) -> list:
    """Every line of the item's attempt history, oldest first: the arm step's own lines and this file's."""
    c = opp.get('commitment') if isinstance(opp.get('commitment'), dict) else {}
    lines = [a for a in (c.get('attempts') or []) if isinstance(a, dict)] + [a for a in (opp.get('attempts') or []) if isinstance(a, dict)]
    return sorted(lines, key=lambda a: str(a.get('first_at') or a.get('at') or ''))


def _merged(lines, entry: dict) -> list:
    """The history with one more attempt in it. A repeat of the same thing in the same place is the same line with its
    count one higher (so a page that fails all night is one line, not a hundred). Lines this file did not write are
    never touched, moved or dropped."""
    out = [dict(a) for a in (lines or []) if isinstance(a, dict)]
    same = next((a for a in out if a.get('by') == BY and a.get('where') == entry['where'] and a.get('what') == entry['what']), None)
    if same is not None:
        same.update(at=entry['at'], why=entry['why'], outcome=entry['outcome'], n=int(same.get('n') or 1) + 1)
        same.setdefault('first_at', entry['at'])
        return out
    out.append(dict(entry, first_at=entry['at'], n=1))
    mine = [a for a in out if a.get('by') == BY]
    for old in mine[:-OWN_LINES]:
        out.remove(old)
    return out


def note(con, opp_key, where, what, why, outcome, now=None, opp=None) -> bool:
    """One attempt, written into the item's ONE history. where: 'worker' | 'watch' | 'monitor' | 'arm' | 'send' |
    'signin' | 'repair'. what: the thing tried, in plain words. why: what went wrong (may be technical: it never
    reaches a note). outcome: how it ended, in plain words.
    An item with a commitment gets the line through commit.py's own bookkeeping write (the version is checked and
    stays as it is; the history table keeps the object). Anything else gets it at payload['attempts'].
    opp: the caller's own copy of the item, kept in step. -> True when the line was written. Never raises."""
    try:
        t = _dt(now)
        row = _item(con, opp_key)
        if row is None:
            return False
        state, p = row[0], row[1]
        entry = {'at': _iso(t), 'where': str(where)[:20], 'what': _one(what, 120), 'why': _one(why, 300), 'outcome': _one(outcome, 160), 'by': BY}
        c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
        if c.get('commitment_id') and state in OPEN:
            import commit as CM
            view = CM.get(con, c['commitment_id'])
            if view is not None and view.get('open'):
                def change(obj, _opp):
                    obj['attempts'] = _merged(obj.get('attempts'), entry)
                CM._book(con, c['commitment_id'], 'attempt', 'recover:' + uuid.uuid4().hex, 'worker' if where == 'worker' else 'tick', t, change,
                         revision=view['revision'], kind='attempt')
                if isinstance(opp, dict):
                    opp['commitment'] = CM._object(con, CM._index(con, c['commitment_id']), CM._opp(con, str(opp_key)))
                return True
        _set(con, opp_key, 'attempts', _merged(p.get('attempts'), entry), opp)
        return True
    except Exception:
        return False                                         # never a rollback here: the caller may hold writes of its own that are not saved yet


def count(opp: dict, where: str, what: str | None = None) -> int:
    """How many attempts of this kind the history holds."""
    n = 0
    for a in history(opp):
        if a.get('where') == where and (what is None or a.get('what') == what):
            n += int(a.get('n') or 1)
        elif where == 'arm' and a.get('what') == 'arm monitor' and not a.get('where'):
            n += 1                                   # the arm step's own lines from before this switch was on
    return n


# ---- the policy's answer -------------------------------------------------------------------------------------------------

def _rank(w: dict) -> int:
    """How authoritative a watched page is: the operator's own page above a tracker. A plain watch has no mark."""
    m = w.get('monitor') if isinstance(w.get('monitor'), dict) else {}
    return 2 if (m.get('trust') or w.get('trust')) == 'source' else 1


def alternative(life, failing: dict):
    """Another page already watched for the SAME item, at least as authoritative, whose last read worked.
    -> that watch's record, or None. Nothing is invented: a page nobody set up is never an alternative."""
    try:
        import watches as W
        d = W.Store(life).load()
        if d is None or not failing.get('item'):
            return None
        for w in d['watches']:
            if (w.get('id') != failing.get('id') and w.get('item') == failing.get('item') and w.get('state') in W.ACTIVE
                    and not int(w.get('fails') or 0) and w.get('value_at') and _rank(w) >= _rank(failing)):
                return w
    except Exception:
        return None
    return None


def next_step(con, opp, failure: str, now=None, *, key=None, life=None, **facts) -> dict:
    """The policy's answer for one failure of one item.
    -> {'failure', 'handling', 'do', 'retry', 'ticket', 'ask', 'when', 'why'} (+ what the row needs: 'source' for an
    alternative, 'sources' for a search, 'cancel' for a stop). `retry` True means: the caller MAY use the one further
    try its own ceiling still has. It is never True past that ceiling, and never for an uncertain effect.
    facts the rows read: fails (reads in a row), watch (the failing watch's record), tries (runs so far), records
    ('none' | 'ran' | 'unreadable'), did (what an interrupted run changed: [] nothing, None could not be read),
    real_step (the run may carry out a step for real), searched (bool), arm_tries, arm_ceiling.
    An unknown failure is refused (ValueError): a caller that cannot name what went wrong must not guess a remedy."""
    if failure not in POLICY:
        raise ValueError(f'"{failure}" is not a failure this policy knows: {", ".join(FAILURES)}')
    t = _dt(now)
    opp = opp if isinstance(opp, dict) else {}
    life = life if life is not None else (_home(con) if con is not None else None)
    rule = POLICY[failure]
    out = {'failure': failure, 'handling': rule['handling'], 'do': rule['do'], 'retry': False, 'ticket': bool(rule['ticket']),
           'ask': bool(rule['ask']), 'when': None, 'why': ''}
    if failure == 'temporary_read_failure':
        if int(facts.get('fails') or 1) > 1:
            return next_step(con, opp, 'repeated_read_failure', t, key=key, life=life, **facts)
        try:
            floor = int(__import__('watches').HOST_FLOOR)
        except Exception:
            floor = 600
        out.update(retry=True, when=_iso(t + timedelta(seconds=floor)), why='one more read, no sooner than the site floor')
    elif failure == 'repeated_read_failure':
        if facts.get('arm_tries') is not None and int(facts['arm_tries']) < int(facts.get('arm_ceiling') or 0):
            out.update(retry=True, why='the arm step has tries left under its own ceiling')
            return out
        alt = alternative(life, facts.get('watch') or {}) if facts.get('watch') else facts.get('alternative')
        if not alt:
            return next_step(con, opp, 'both_routes_unavailable', t, key=key, life=life, **facts)
        out.update(source={'id': alt.get('id'), 'for': _one(alt.get('for'), 120)}, why='another page for the same job, as authoritative, is being read')
    elif failure == 'both_routes_unavailable':
        out.update(why='no other page for this job is readable: it waits, with one repair ticket')
    elif failure == 'signin_expired':
        out.update(helper='relogin' if life is not None and (Path(life) / 'relogin-on').exists() else 'their own sign-in', types_code=False,
                   why='the sign-in helper tries once where no code is asked; otherwise the owner signs in once')
    elif failure == 'access_needed':
        out.update(resume='when access is verified', why='only the owner can give this access: one prepared request')
    elif failure in ('job_never_started', 'job_interrupted'):
        tries = int(facts.get('tries') or 1)
        if tries >= WORKER_RUNS:
            out.update(why='both runs are used')
        elif failure == 'job_never_started':
            records = facts.get('records')
            if records == 'none' or (records == 'unreadable' and not facts.get('real_step')):
                out.update(retry=True, why='the run records show no run' if records == 'none' else
                           'the run records could not be read; the work only reads and drafts, so one more run is safe')
            else:
                out.update(do='settle_by_reading', why='the run records could not be read and the run may carry out a step for real')
        else:
            did = facts.get('did', [])
            changed = None if did is None else [str(x) for x in did if not str(x).startswith('private read')]
            if changed:
                out.update(do='settle_by_reading', ticket=True, why='the interrupted run had already sent or changed something')
            elif changed is None and facts.get('real_step'):
                out.update(do='settle_by_reading', why='what the interrupted run did could not be read, and it may have carried out a step for real')
            else:
                out.update(retry=True, safe_work='only what is left: nothing already done is done again',
                           why='the interrupted run changed nothing' if changed is not None else 'what it did could not be read; the work only reads and drafts')
    elif failure == 'uncertain_effect':
        out.update(why='it may have gone: only reading the destination settles it')
    elif failure == 'missing_fact':
        searched = bool(facts.get('searched')) or any(a.get('what') == SEARCHED for a in history(opp))
        if searched or int(facts.get('tries') or 1) >= WORKER_RUNS:
            out.update(do='ask_once', retry=False, why='the permitted sources were searched (or both runs are used): the owner is asked, once')
        else:
            out.update(retry=True, ask=False, sources=list(PERMITTED), why='search before asking')
    elif failure == 'budget_exhausted':
        midnight = (t.astimezone(_tz()) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        d = deadline(opp)
        look = min(midnight, d - timedelta(seconds=NEAR)) if d is not None else midnight
        out.update(when=_iso(look) if look > t else None, why='the budget resets at midnight' if look == midnight else 'its deadline is near')
    elif failure == 'stopped':
        out.update(cancel=['later checks', 'steps waiting for their yes', 'watches', 'a standing OK that was taken back'], why='the owner said stop, or took the permission back')
    return out


# ---- escalation: facts for the writer, never sentences glued on ----------------------------------------------------------

def _tried(opp: dict, now) -> list:
    out = []
    arm = [a for a in history(opp) if a.get('what') == 'arm monitor']
    if arm:
        out.append(f"Start of the watch: tried {_times(len(arm))}, last {_clock(arm[-1].get('at'), now)}; the page could not be read.")
    for a in history(opp):
        if a.get('by') != BY:
            continue
        what = _one(a.get('what') or WHAT.get(a.get('where'), 'attempt'), 120)
        out.append(f"{what[:1].upper()}{what[1:]}: tried {_times(a.get('n') or 1)}, last {_clock(a.get('at'), now)}; {_one(a.get('outcome'), 160) or 'it did not work'}.")
    return out[:6]


def escalation(opp: dict, now=None) -> dict:
    """Plain facts for the note writer about an item recovery could not finish:
      unfinished   what is not done (the item's own title)
      tried        what was already tried, from the attempt history, one plain line each
      needed       the exact thing the owner must provide or decide (None when nothing of the owner's would help)
      then         what Zeus does straight afterwards
    {} for an item with no open failure. No ids, no field names, no error text."""
    opp = opp if isinstance(opp, dict) else {}
    kept = opp.get('recover') if isinstance(opp.get('recover'), dict) else {}
    if isinstance(kept.get('facts'), dict):
        return dict(kept['facts'])                         # an escalation item carries the facts it was raised with
    s = _state(opp)
    failure = s.get('failure')
    if failure not in POLICY:
        return {}
    t = _dt(now)
    return {'unfinished': _one(opp.get('title'), 160), 'tried': _tried(opp, t), 'needed': s.get('needed') or None,
            'then': s.get('then') or THEN.get(failure) or 'Nothing more happens by itself.'}


_COMMON = frozenset(('your', 'this', 'that', 'with', 'from', 'have', 'will', 'once', 'yourself', 'tell', 'whether', 'there', 'where', 'would', 'gone',
                     'look', 'finish', 'never', 'those', 'them', 'type', 'asks', 'sent', 'and', 'the', 'for', 'own', 'you', 'not'))


def ask_in(text: str, needed: str) -> bool:
    """Is the ask still in the written note? A word check: the note must carry the words that make this ask this ask
    (at least two of them, or all when there are fewer). Code checks; it never writes the ask in itself."""
    words = {w for w in re.findall(r'[a-z0-9]{3,}', str(needed or '').lower())} - _COMMON
    if not words:
        return True
    low = str(text or '').lower()
    hits = sum(1 for w in words if w in low)
    return hits >= min(2, len(words))


def _ticket(life, key, title: str, detail) -> bool:
    try:
        return __import__('worker')._alarm(life, key, title, kind='zeus_recovery', detail=detail) is not False
    except Exception:
        return False


def open_failure(con, life, key, failure: str, now, *, problem: str, needed=None, then=None, ticket=None, opp=None, **more) -> dict:
    """Write an open failure on the item (what, since when, the concrete problem, what the owner could give) and raise its ONE
    repair ticket when the policy says so. Asking the owner is escalate()'s job. -> the item's recovery state."""
    t = _dt(now)
    row = _item(con, key)
    if row is None:
        return {}
    p = row[1]
    s = _state(p)
    same = s.get('failure') == failure
    s.update(failure=failure, since=s.get('since') if same and s.get('since') else _iso(t), problem=_one(problem, 300),
             needed=_one(needed, 300) or None, then=_one(then or THEN.get(failure), 300) or None, **{k: v for k, v in more.items() if v is not None})
    ident = identity(p, key, failure)
    want = POLICY[failure]['ticket'] if ticket is None else ticket
    if want and ident not in (s.get('tickets') or {}):
        s.setdefault('tickets', {})[ident] = {'at': _iso(t), 'taken': _ticket(life, key, f"Zeus could not finish: {_one(p.get('title'), 100)}", [problem, f'failure: {failure}'])}
    _set(con, key, 'recovery', s, opp)
    return s


def clear(con, key, why: str, now=None, opp=None) -> bool:
    """The failure is over (a read worked, the sign-in is back): the item has no open failure any more, and an ask about
    it that the owner has not been told yet closes without a word. What was asked stays on record."""
    row = _item(con, key)
    if row is None:
        return False
    s = _state(row[1])
    if not s.get('failure'):
        return False
    t = _dt(now)
    for ident, a in (s.get('asked') or {}).items():
        child = a.get('child')
        if child and not a.get('answered'):
            got = _item(con, child)
            if got and got[0] in ('pending', 'ready'):
                cp = got[1]
                cp['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'recover', 'why': _one(why, 200)}
                con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=? AND state=?", (json.dumps(cp, default=str), time.time(), child, got[0]))
            a['answered'] = _iso(t)
    s.update(failure=None, cleared={'at': _iso(t), 'why': _one(why, 200)})
    for k in ('problem', 'needed', 'then', 'since', 'watch', 'site', 'waiting', 'arm', 'covered_by'):
        s.pop(k, None)
    _set(con, key, 'recovery', s, opp)
    return True


def escalate(con, life, key, now, own: bool = False) -> str:
    """Recovery is out of road for this item's open failure: tell the owner, by the rule.
      own=True   the item itself is going to the owner with its problem (the worker readied it without the prep). Nothing is
                 added and its time is left alone; when its deadline is within two hours it is marked urgent, so the
                 quiet-hour code looks at it tonight instead of holding it as routine.
      otherwise  ONE small ready item of its own, a child of the item, carrying the facts:
                 deadline within two hours: due now, urgent (notifier.py and policy.py still decide the hour);
                 later or unknown: only when there is something the owner can give, due at the next morning note or two
                 hours before the deadline, whichever comes first. With nothing useful to ask, only the ticket stays.
    The same ask (commitment id, version, failure) is never raised twice.
    -> 'own' | the child's key | '' (already asked, nothing to ask yet, or no open failure)."""
    t = _dt(now)
    row = _item(con, key)
    if row is None:
        return ''
    state, p = row[0], row[1]
    s = _state(p)
    failure = s.get('failure')
    if failure not in POLICY or state not in OPEN:
        return ''
    ident = identity(p, key, failure)
    asked = s.setdefault('asked', {})
    is_near = near(p, t)
    if own:
        if ident not in asked or asked[ident].get('answered'):
            asked[ident] = {'at': _iso(t), 'own': True, 'near': is_near}
            _set(con, key, 'recovery', s)
        if is_near:
            con.execute("UPDATE opps SET urgency='now' WHERE key=? AND state='ready'", (str(key),))
        return 'own'
    if ident in asked and not asked[ident].get('answered'):
        return ''
    if not is_near and not s.get('needed'):
        return ''                                           # nothing useful to ask and no deadline close: the ticket stays, the owner is not interrupted
    d = deadline(p)
    try:
        brief = __import__('notifier').next_daytime(t.astimezone(_tz()))
    except Exception:
        brief = (t.astimezone(_tz()) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    when = t if is_near else (min(brief, d - timedelta(seconds=NEAR)) if d is not None else brief)
    facts = {'unfinished': _one(p.get('title'), 160), 'tried': _tried(p, None), 'needed': s.get('needed') or None,     # kept with the item and read later: full dates
             'then': s.get('then') or THEN.get(failure) or 'Nothing more happens by itself.'}
    child = f"{str(key).split('#')[0]}#fu-recover:{hashlib.sha1(ident.encode()).hexdigest()[:8]}"
    if con.execute('SELECT 1 FROM opps WHERE key=?', (child,)).fetchone() is None:
        headline = s.get('problem') or f"Not finished: {facts['unfinished']}"
        step = {'do': headline, 'action_class': 'notify_owner_now', 'autonomy': 'auto'}
        decision = {'ask': facts['needed'], 'ready': facts['then'], 'default': '', 'by': 'owner'} if facts['needed'] else None
        payload = {'title': 'Something I could not finish: ' + facts['unfinished'], 'why': _one(p.get('title'), 160), 'evidence': '',
                   'urgency': 'now' if is_near else 'today', 'dedupe_key': child, 'parent': key, '_source': p.get('_source'), 'owner_involved': 'yes',
                   'confidence': 1.0, 'steps': [step], 'ready_at': t.timestamp(),
                   'followup': {'check': headline, 'when': _iso(when), 'reminder': True},
                   'decision': {'autonomy': 'auto', 'surface_at': _iso(when), 'channel': 'text_now' if is_near else 'text', 'steps': [step], 'granted': False,
                                'reason': 'recovery could not finish this'},
                   'packet': {'status': 'needs_decision' if decision else 'done', 'headline': headline, 'details': '', 'decision': decision,
                              'followups': [], 'learned': []},
                   'recover': {'parent': key, 'failure': failure, 'identity': ident, 'facts': facts,
                               'near_ts': (d - timedelta(seconds=NEAR)).timestamp() if d is not None else None}}
        for k in ('deadline', 'event_start'):
            if p.get(k):
                payload[k] = p[k]                             # the quiet-hour code judges the night by the thing's own times
        con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)', (child, 'ready', 'auto', _iso(when), 'now' if is_near else 'today', 'notify_owner_now',
                                                                   json.dumps(payload, default=str), '[]', time.time(), time.time()))
    asked[ident] = {'at': _iso(t), 'child': child, 'near': is_near, 'due': _iso(when)}
    _set(con, key, 'recovery', s)
    return child


# ---- notifier.py: the facts reach the writer, and code checks the ask ----------------------------------------------------

SAY_RULE = ('\n- An item may carry "still_unfinished", "already_tried", "what_i_need_from_him" and "what_i_do_next": something you could not finish. '
            'Say plainly what is not done and, when there is one, the one thing you need from the owner, in your own words, once. '
            'Do not list the tries; one short clause is enough. Never write as if it were done or under way.')
MISSING_WHY = 'the note must still carry this one request to the owner, in your own words: {needed}'
FILE_RULE = ('\n- An item may carry "file_for_him": a file you made for the owner, what kind it is and where it stands. Say exactly where it stands. '
             '"Prepared" is not "sent": if it has not been delivered, say it is ready and that getting it to the owner is still open, and why. '
             'Never give the owner a folder or file path on your Mac: their phone cannot open it.')
PATH_WHY = 'this is a path on your own Mac; their phone cannot open it. Say the file is ready and where it stands instead.'


def _facts_view(opp: dict, now: datetime) -> dict:
    pk = opp.get('packet') if isinstance(opp.get('packet'), dict) else {}
    if not isinstance(opp.get('recover'), dict) and not (pk.get('prep') or pk.get('status') == 'blocked'):
        return {}                                           # its result is whole: a failure written on it earlier is history, not a fact about this note
    f = escalation(opp, now)
    if not f:
        return {}
    return {k: v for k, v in (('still_unfinished', f.get('unfinished')), ('already_tried', f.get('tried')),
                              ('what_i_need_from_him', f.get('needed')), ('what_i_do_next', f.get('then'))) if v}


def composing(con, life, now_dt, compose, keep: list, out: dict):
    """notifier.deliver: the writer for this note. Without the switch: the writer it was given. With it, each item that
    carries an open failure is handed its facts (escalation), plus one short rule and, for a writer that can take it,
    the check for its one targeted fix (the ask must still be in the note). Nothing is appended to what it writes."""
    try:
        compose = __import__('capability').composing(con, life, now_dt, compose, keep, out)   # switch capability-on: a source that could not be read; the same writer without it
    except Exception:
        pass
    if not on(life):
        return compose
    try:
        t = _dt(now_dt)
        by_title, files = {}, False
        for r, o in keep:
            v = _facts_view(o, t)
            try:
                import artifact as AF
                c = o.get('commitment') if isinstance(o.get('commitment'), dict) else {}
                mine = [d for d in (c.get('deliverables') or o.get('deliverables') or []) if isinstance(d, dict)]
                if mine:
                    v = dict(v, file_for_him=[AF.for_writer(d, life) for d in mine][:3])
                    files = True
            except Exception:
                pass
            if v:
                by_title[str(o.get('title'))] = v
        if not by_title:
            return compose
        import inspect
        try:
            takes = inspect.signature(compose).parameters
        except (TypeError, ValueError):
            takes = {}
        any_kw = any(p.kind == p.VAR_KEYWORD for p in takes.values())

        def wrapped(items, when, recent, **kw):
            shown = [({**it, **by_title[str(it.get('item'))]} if isinstance(it, dict) and str(it.get('item')) in by_title else it) for it in items]
            if 'rules' in takes or any_kw:
                kw['rules'] = str(kw.get('rules') or '') + SAY_RULE
                if files:
                    kw['rules'] += FILE_RULE
                    if (Path(life) / 'file-delivery-on').exists():   # filedeliver.py: one more sentence, for a file that rides this note as an attachment. Nothing without the switch
                        kw['rules'] += __import__('filedeliver').WRITER_RULE
            needs = [v['what_i_need_from_him'] for v in by_title.values() if v.get('what_i_need_from_him')]
            if (needs or files) and ('must_say' in takes or any_kw):
                earlier = kw.get('must_say')
                kw['must_say'] = lambda text: (list(earlier(text)) if earlier else []) + [
                    {'quote': n[:120], 'why': MISSING_WHY.format(needed=n)} for n in needs if not ask_in(text, n)] + (
                    [{'quote': q[:120], 'why': PATH_WHY} for q in __import__('artifact').local_paths(text)] if files else [])
            out['recover_facts'] = len(by_title)
            return compose(shown, when, recent, **kw)
        try:
            wrapped.__signature__ = inspect.signature(compose)      # fresh.composing asks what this writer can take: the same as the one inside
        except (TypeError, ValueError):
            pass
        return wrapped
    except Exception as e:
        out.setdefault('recover_errors', []).append(f'facts: {type(e).__name__}: {str(e)[:80]}')
        return compose


def after_note(con, life, text, carried: list, note_key: str, now_dt, out: dict) -> None:
    """notifier.deliver, once the note is on its way: for every carried item with an ask of recovery's, code checks the
    ask is in the words that went, and writes down that it was asked (so it is never asked again unchanged). An ask the
    writer left out was NOT asked: that is written down too, and the item goes round once more to the next morning
    note. Never raises."""
    if not on(life):
        return
    try:
        t = _dt(now_dt)
        for key in carried:
            row = _item(con, key)
            if row is None:
                continue
            p = row[1]
            mark = p.get('recover') if isinstance(p.get('recover'), dict) else None
            parent = mark.get('parent') if mark else key
            got = _item(con, parent) if mark else row
            if got is None:
                continue
            s = _state(got[1])
            ident = mark.get('identity') if mark else next((i for i, a in (s.get('asked') or {}).items() if a.get('own') and not a.get('told')), None)
            if not ident or ident not in (s.get('asked') or {}):
                continue
            needed = (escalation(p, t) or {}).get('needed')
            said = ask_in(text, needed) if needed else True
            a = s['asked'][ident]
            a.update(told=_iso(t), note=note_key, in_note=bool(said))
            _set(con, parent, 'recovery', s)
            out['recover_asked'] = out.get('recover_asked', 0) + 1
            if not said:
                out['recover_ask_left_out'] = out.get('recover_ask_left_out', 0) + 1
                if mark and not mark.get('again'):
                    p['recover'] = dict(mark, again=_iso(t))
                    p.pop('told', None)
                    brief = __import__('notifier').next_daytime(t.astimezone(_tz()))
                    con.execute("UPDATE opps SET state='ready', surface_at=?, payload=?, updated=? WHERE key=?", (_iso(brief), json.dumps(p, default=str), time.time(), key))
        con.commit()
    except Exception as e:
        out.setdefault('recover_errors', []).append(f'asked: {type(e).__name__}: {str(e)[:80]}')


# ---- worker.py -----------------------------------------------------------------------------------------------------------

def _life_of_worker():
    try:
        return getattr(__import__('commit'), '_FENCE_HOME', None)      # the folder worker.run said it is working in
    except Exception:
        return None


def prompt_line(opp: dict, now_dt=None) -> str:
    """worker.build_prompt: plain sentences for the job, only when they apply; '' otherwise, and always '' without the
    switches. recover-on: a run that follows an interrupted one is told to do only what is left; a run that follows
    "blocked with no search" is told to search the permitted sources first; a run that follows the owner's answer is given it.
    capability-on: only the capabilities THIS job needs that are not available (capability.prompt_lines)."""
    life = _life_of_worker()
    text = ''
    try:
        if on(life):
            ex = opp.get('exec') if isinstance(opp.get('exec'), dict) else {}
            s = _state(opp)
            if ex.get('resume_safe'):
                text += ('An earlier run on this was cut short before it answered. Before you do anything again, look at what is already done '
                         '(drafts, notes and anything already written for it) and do only what is left.\n')
            if ex.get('search_first'):
                text += ('Your earlier run said something only the owner can supply was missing, without saying where it had looked. Before you report '
                         'anything as missing, search for it in ' + ', '.join(PERMITTED[:-1]) + ' and ' + PERMITTED[-1] + '. Ask the owner only for what none of '
                         'them has, and list where you looked under "sources".\n')
            said = s.get('supplied') if isinstance(s.get('supplied'), dict) else None
            if said and said.get('words'):
                text += 'You asked the owner for something on this and the owner has answered: "%s". Use it and do not ask again.\n' % _one(said['words'], 300)
    except Exception:
        pass
    try:
        text += __import__('capability').prompt_lines(life, opp, now_dt)
    except Exception:
        pass
    return text


def blocked_first(life, key, opp: dict, ex: dict, packet, can_work: bool):
    """worker._harvest_one, with a job's answer in hand. Missing fact: search the permitted sources before asking the owner.
    A first run that reports "blocked" and names nowhere it looked is not taken as the answer: it is handed back as no
    answer, so the worker's own one recovery run (never a third) searches first. Everything else, the second run, an
    item whose run may carry out a step for real, and anything at all without the switch: the same packet."""
    try:
        if not on(life) or not isinstance(packet, dict) or packet.get('status') != 'blocked':
            return packet
        if int(ex.get('tries', 1)) >= WORKER_RUNS or not can_work or packet.get('sources') or __import__('worker').standing_ok(opp):
            if isinstance(opp, dict):
                d = packet.get('decision') or {}
                s = _state(opp)
                at = _iso(_dt())
                s.update(failure='missing_fact', since=at, needed=_one(d.get('ask') or packet.get('headline'), 300) or None, then=THEN['missing_fact'],
                         problem=_one(packet.get('headline'), 300) or None)
                s.setdefault('asked', {})[identity(opp, key, 'missing_fact')] = {'at': at, 'own': True}     # the item itself carries the ask to the owner, once
                opp['recovery'] = s
                if not packet.get('decision') and s.get('needed'):
                    # blocked on the owner IS waiting on the owner: without this the item was closed as "done" the moment the owner was told,
                    # and the owner's answer had nothing open to go back to
                    packet['decision'] = {'ask': s['needed'], 'ready': THEN['missing_fact'], 'default': '', 'by': 'owner'}
            return packet
        ex['search_first'] = True
        return None
    except Exception:
        return packet


def job_failed(con, life, key, opp: dict, ex: dict, can_work: bool, now_dt, res: dict, *, status=None, rows=None, gone=None, stalled=False,
               err=None, audit=None, response=None) -> bool:
    """worker._harvest_one, where a run is found failed, never started, stalled or without an answer; called just before
    the worker's own ceiling check. Writes the attempt into the item's history, asks the policy, and hands back
    `can_work`: unchanged when the one recovery run may be used, False when the policy says it must not (the run may
    already have done something). It can only take the recovery run away, never add one. Without the switch: the same
    value, nothing written."""
    if not on(life):
        return can_work
    try:
        tries = int(ex.get('tries', 1))
        real = bool(__import__('worker').standing_ok(opp))
        if ex.get('search_first') and tries < WORKER_RUNS:
            failure, facts, how = 'missing_fact', {'tries': tries}, 'it reported something missing without searching'
        elif (rows is None or not rows) and gone != 'cut short':          # no run on record (or the records could not be read)
            records = 'unreadable' if rows is None else 'none'
            if rows is None and response is not None and ex.get('job_id'):
                try:
                    records = 'ran' if response(ex['job_id']) else 'unreadable'
                except Exception:
                    records = 'unreadable'
            failure, facts, how = 'job_never_started', {'tries': tries, 'records': records, 'real_step': real}, 'it never started'
            if records == 'ran':
                failure, how = 'job_interrupted', 'it started and did not finish'
        else:
            failure, facts, how = 'job_interrupted', {'tries': tries, 'real_step': real}, 'it did not finish'
        if failure == 'job_interrupted':
            did = None
            if audit is not None and ex.get('job_id'):
                try:
                    did = audit(ex['job_id'])
                except Exception:
                    did = None
            facts.update(did=did, real_step=real)
        step = next_step(con, opp, failure, now_dt, key=key, life=life, **facts)
        note(con, key, 'worker', 'background run', 'it said something was missing' if failure == 'missing_fact' else
             str(err or ex.get('no_result') or status or ex.get('dropped') or 'never ran'), how, now_dt, opp=opp)
        ex['recovery'] = {'failure': failure, 'do': step['do'], 'why': step['why']}
        ex.pop('resume_safe', None)
        if step['retry'] and failure == 'job_interrupted':
            ex['resume_safe'] = True
        if failure == 'job_interrupted' and step['do'] == 'settle_by_reading' and facts.get('did'):
            changed = [str(x)[:200] for x in facts['did'] if not str(x).startswith('private read')][:5]
            if changed and not real:
                ex['broke_a_limit'] = changed                 # a failed run is audited too, not only a finished one
                res['limit_breaks'] = res.get('limit_breaks', 0) + 1
                try:
                    W = __import__('worker')
                    W._alarm(life, key, W.broke_title(opp, changed), kind='life_job_broke_limit', detail=changed)
                except Exception:
                    pass
        res['recover'] = res.get('recover', 0) + 1
        return bool(can_work and step['retry'])
    except Exception as e:
        res.setdefault('errors', []).append(f'recover {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')
        return can_work


def gave_up(con, life, key, failure: str, now_dt, res: dict, *, problem: str = '', needed=None, **more) -> None:
    """A caller has just readied an item without its prep because its last try failed (worker: both runs; signedin: the
    sign-in was not renewed in time). The item itself is the message: its failure is written on it, the facts ride with
    it to the writer, and the two-hour rule marks it urgent when its deadline is that close. Nothing without the
    switch. Never raises."""
    if not on(life):
        return
    try:
        row = _item(con, key)
        if row is None or row[0] not in OPEN:
            return
        title = _one(row[1].get('title'), 120)
        open_failure(con, life, key, failure if failure in POLICY else 'job_interrupted', now_dt,
                     problem=problem or f'I could not finish the background work on: {title}', needed=needed, ticket=False, **more)
        if escalate(con, life, key, now_dt, own=True):
            res['recover_escalated'] = res.get('recover_escalated', 0) + 1
    except Exception as e:
        res.setdefault('errors', []).append(f'recover {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')


def stay_pending(con, life, key, opp: dict, now_dt, res: dict) -> bool:
    """worker.run, for a due item when the day's work budget is used up. Policy: stay pending and look again when the
    budget resets or the deadline nears, instead of telling the owner at once without the prep. True = it was put back to
    wait (the worker readies nothing). False = no later moment is left (the deadline is inside two hours), or the
    switch is absent: the worker does what it always did."""
    if not on(life):
        return False
    try:
        step = next_step(con, opp, 'budget_exhausted', now_dt, key=key, life=life)
        if not step['when']:
            # no later moment is left: the worker tells the owner without the prep, as always. Inside two hours of its deadline
            # the item is marked urgent, so the quiet-hour code looks at it tonight instead of holding it as routine.
            if near(opp, _dt(now_dt)):
                s = _state(opp)
                s.update(failure='budget_exhausted', since=_iso(_dt(now_dt)), problem='The day\'s background work was used up, so this was not worked on.')
                s.setdefault('asked', {})[identity(opp, key, 'budget_exhausted')] = {'at': _iso(_dt(now_dt)), 'own': True, 'near': True}
                opp['recovery'] = s
                con.execute("UPDATE opps SET urgency='now' WHERE key=?", (str(key),))
            return False
        s = _state(opp)
        first = not (s.get('budget') or {}).get('since')
        s['budget'] = {'since': (s.get('budget') or {}).get('since') or _iso(_dt(now_dt)), 'look_again': step['when'], 'why': step['why']}
        cur = con.execute("UPDATE opps SET surface_at=?, payload=json_set(payload,'$.recovery',json(?)), updated=? WHERE key=? AND state='pending'",
                          (step['when'], json.dumps(s, default=str), time.time(), str(key)))
        if cur.rowcount != 1:
            return False
        if first:
            note(con, key, 'worker', 'background run', 'the daily work budget is used up', 'not started: it waits for the budget', now_dt)
        res['recover_waiting_for_budget'] = res.get('recover_waiting_for_budget', 0) + 1
        return True
    except Exception as e:
        res.setdefault('errors', []).append(f'recover {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')
        return False


# ---- watches.py: a read that failed --------------------------------------------------------------------------------------

def read_failed(con, life, w: dict, result, now_dt, out: dict) -> None:
    """watches.run, after one read was taken into its record. Unknown (the read failed, or the part that matters was not
    found): the attempt goes into the history of the item the watch belongs to, and the policy is asked.
      first failure    a plain watch is read once more at the site floor instead of after its 30-minute backoff
                       (a monitor already retries at its own cadence, which is never under the floor)
      again            another page already watched for the same job, as authoritative and readable: nothing more
      no such page     it waits, with ONE repair ticket for this outage; for a plain watch that belongs to an item, the
                       item gets the open failure, and escalate() follows the two-hour rule (a monitor raises its own
                       "can't read it" notice, so recovery does not ask about it a second time)
    A read that works again ends the outage (clear). Nothing without the switch. Never raises."""
    if not on(life):
        return
    try:
        import watches as W
        store = W.Store(life)
        d = store.load()
        rec = W._find(d, w.get('id')) if d else None
        if rec is None:
            return
        key = rec.get('item') or ''
        t = _dt(now_dt)
        if result in ('changed', 'unchanged'):
            if (rec.get('recovery') or {}).get('outage'):
                store.change(lambda data: (W._find(data, w['id']) or {}).pop('recovery', None))
                if key and con is not None and _state((_item(con, key) or (None, {}))[1]).get('watch') == rec.get('id'):
                    clear(con, key, 'the page could be read again', t)
            return
        if result != 'unknown':
            return
        fails = int(rec.get('fails') or 0)
        opp = (_item(con, key) or (None, {}))[1] if key and con is not None else {}
        step = next_step(con, opp, 'temporary_read_failure', t, key=key, life=life, fails=fails, watch=rec)
        if key and con is not None:
            note(con, key, 'monitor' if rec.get('monitor') else 'watch', f'read of the page for "{_one(rec.get("for"), 80)}"',
                 rec.get('unknown_why') or 'the read failed', 'could not be read', t)
        outage = str(rec.get('unknown_since') or _iso(t))
        mark = dict(rec.get('recovery') or {})
        if step['do'] == 'retry_read':
            if not rec.get('monitor'):
                def sooner(data):
                    r = W._find(data, w['id'])
                    cur = W._dt(r.get('next_check')) if r else None
                    if r is not None and (cur is None or cur > _dt(step['when'])):
                        r['next_check'] = step['when']
                store.change(sooner)
                out['recover_retry'] = out.get('recover_retry', 0) + 1
            return
        if step['do'] == 'alternative_source':
            if mark.get('covered_by') != step['source']['id']:
                store.change(lambda data: (W._find(data, w['id']) or {}).__setitem__('recovery', {'outage': outage, 'covered_by': step['source']['id']}))
            out['recover_alternative'] = out.get('recover_alternative', 0) + 1
            return
        if mark.get('outage') == outage and mark.get('ticket'):
            return                                           # this outage already has its one ticket
        what = _one(rec.get('for'), 100)
        took = _ticket(life, key or f"watch:{rec.get('id')}", f'Zeus cannot read a page it is watching: {what}',
                       [f"unreadable since {outage}", f"reads failed in a row: {fails}", f"why: {_one(rec.get('unknown_why'), 200)}"])
        store.change(lambda data: (W._find(data, w['id']) or {}).__setitem__('recovery', {'outage': outage, 'ticket': _iso(t), 'taken': took}))
        out['recover_ticket'] = out.get('recover_ticket', 0) + 1
        if key and con is not None and not rec.get('monitor'):
            open_failure(con, life, key, 'both_routes_unavailable', t, ticket=False, watch=rec.get('id'),
                         problem=f'I cannot read the page I watch for "{what}", and there is no other page for it. Nobody is watching it right now.')
    except Exception as e:
        out.setdefault('errors', []).append(f'recover: {type(e).__name__}: {str(e)[:120]}')


# ---- commit.py: the arm step ---------------------------------------------------------------------------------------------

def arm_stops(life, obj: dict, opp: dict, failed: list, stop: bool, now) -> bool:
    """commit._armed, where the arm step decides whether it has tried enough (inside its own transaction: nothing is
    written here but the object and the item it already holds). The line it just added to the history says where it
    came from; the policy is asked; and when the arm step stops, the item carries the open failure so tend() can follow
    the two-hour rule. -> `stop` as the arm step worked it out, or True sooner when the owner has stopped the pipes (`off`).
    Never False when the arm step said True: recovery cannot add a try. Without the switch: `stop`."""
    if not on(life):
        return stop
    try:
        t = _dt(now)
        lines = [a for a in obj.get('attempts') or [] if isinstance(a, dict) and a.get('what') == 'arm monitor']
        if lines:
            lines[-1].update(where='arm', outcome='the page could not be read')
        import commit as CM
        legs = [l for l in (((obj.get('next_action') or {}).get('monitor') or {}).get('legs') or []) if isinstance(l, dict)]
        missing = [l for l in legs if not l.get('watch') and not l.get('stopped')]
        covered = next((l for l in legs if l.get('watch') and not l.get('stopped') and _rank(l) >= max([_rank(m) for m in missing] or [1])), None)
        step = next_step(None, opp.get('payload') or {}, 'repeated_read_failure', t, life=life, arm_tries=len(lines), arm_ceiling=0 if stop else CM.ARM_TRIES + 1,
                         alternative={'id': covered.get('watch'), 'for': covered.get('for')} if covered else None)
        if (Path(life) / 'off').exists():
            stop = True                                       # STOP: the affected work stops; no further try
        if stop:
            p = opp.get('payload') if isinstance(opp.get('payload'), dict) else {}
            s = _state(p)
            title, why = _one(p.get('title'), 100), _one('; '.join(failed), 160)
            s.update(failure=step['failure'], since=_iso(t), needed=None, arm=True, then='I am not trying to start it again by myself. It is flagged for repair.',
                     problem=f'I could not start watching for "{title}" ({why}). It is not being watched.')
            s.pop('covered_by', None)
            if step.get('source'):
                s.update(covered_by=step['source'], problem=f'I could not start watching one page for "{title}" ({why}). '
                                                            f"The other page for it, \"{_one(step['source'].get('for'), 100)}\", is being watched.")
            p['recovery'] = s
        return stop
    except Exception:
        return stop


# ---- onesend.py: an unknown send -----------------------------------------------------------------------------------------

def settled(con, life, rec: dict, outcome: str, now_dt, out: dict) -> None:
    """onesend.tend, right after an unknown email was read back (settle). The read-back is the attempt; the policy's
    answer for an uncertain effect is always "settle by reading", never another send, and that is checked here: if any
    row ever said otherwise this raises inside its own guard and counts an error instead of letting a send through.
    Still unknown after SETTLE_READS reads: the owner is asked, once, to look at the thread themselves (the two-hour rule times
    it). Settled either way: the open failure is cleared. Nothing without the switch. Never raises."""
    if not on(life):
        return
    try:
        key = rec.get('item')
        row = _item(con, key)
        if row is None:
            return
        t = _dt(now_dt)
        step = next_step(con, row[1], 'uncertain_effect', t, key=key, life=life)
        if step['retry'] or step['do'] != 'settle_by_reading':
            raise RuntimeError('the policy answered a retry for an uncertain send')
        said = {'sent': 'it is there: it went', 'did_not_run': 'it is not there: it did not go', 'other': 'a different reply from their mailbox is there'}
        note(con, key, 'send', WHAT['send'], f'the send gave no clear answer ({rec.get("id")})', said.get(outcome, 'still cannot tell'), t)
        out['recover_settled'] = out.get('recover_settled', 0) + 1
        if outcome in ('sent', 'did_not_run', 'other'):
            clear(con, key, 'the thread was read: ' + said[outcome], t)
            return
        got = _item(con, key)
        reads = count(got[1], 'send') if got else 0
        if reads >= SETTLE_READS or near(row[1], t):
            who = _one(', '.join((rec.get('details') or {}).get('to') or []) or 'the other side', 120)
            open_failure(con, life, key, 'uncertain_effect', t, ticket=False,
                         problem=f'A reply you approved may or may not have gone to {who}: I read the thread {_times(max(reads, 1))} and cannot tell.',
                         needed=NEEDED['uncertain_effect'].format(where=f'your thread with {who}'))
            if escalate(con, life, key, t):
                out['recover_escalated'] = out.get('recover_escalated', 0) + 1
    except Exception as e:
        out.setdefault('errors', []).append(f'recover: {type(e).__name__}: {str(e)[:120]}')


# ---- worker.py, around signedin.hold: held, back, not renewed --------------------------------------------------------------

def _relogin_stop(life, site: str, now: datetime):
    """Why Zeus's own try at signing back in stopped today, when it stopped at something only the owner can pass."""
    try:
        rows = [json.loads(ln) for ln in (Path(life) / 'relogin.jsonl').read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return None
    day = now.astimezone(_tz()).date().isoformat()
    last = next((r for r in reversed(rows) if isinstance(r, dict) and r.get('store') == site and r.get('event') == 'finished' and r.get('day') == day), None)
    return last.get('stop') if last and last.get('stop') in ACCESS_STOPS else None


def after_hold(con, life, key, opp: dict, now_dt, res: dict, held: bool) -> bool:
    """worker.run, around its one call to signedin.hold. signedin.py is not touched: what it just did is read from the
    marks it leaves on the item (held_for_signin; signin_released or signin_not_renewed stamped with this very run).
      held          the hold goes into the item's history, once (signin_held)
      gave up       signedin told the owner without the prep because the sign-in was not renewed in time: the item carries the
                    open failure, so a sign-in that returns later sends the SAME item back to work once (tend)
      let go        signed in again: the same item goes on, and its history says so (signin_back)
    -> `held`, exactly as signedin.hold answered. Without the switch nothing else happens. Never raises."""
    if not on(life):
        return held
    try:
        import signedin as S
        import watches as W
        stamp = W._iso(now_dt)
        name_of = lambda site: str((S.sites(life).get(site) or {}).get('name') or site)
        mark = opp.get('held_for_signin')
        gone = opp.get('signin_not_renewed') if isinstance(opp.get('signin_not_renewed'), dict) else {}
        back = opp.get('signin_released') if isinstance(opp.get('signin_released'), dict) else {}
        if held and isinstance(mark, dict) and mark.get('site'):
            signin_held(con, life, key, opp, mark['site'], name_of(mark['site']), now_dt, res)
        elif held and gone.get('at') == stamp and gone.get('site'):
            name = name_of(gone['site'])
            gave_up(con, life, key, 'signin_expired', now_dt, res, problem=f'My {name} sign-in lapsed and was not renewed in time, so this was not done.',
                    needed=NEEDED['signin_expired'].format(name=name), site=gone['site'], waiting={'for': 'signin', 'site': gone['site'], 'name': name, 'since': stamp})
        elif not held and back.get('at') == stamp:
            signin_back(con, life, key, opp, now_dt, res)
    except Exception as e:
        res.setdefault('errors', []).append(f'recover {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')
    return held


def signin_held(con, life, key, opp: dict, site: str, name: str, now_dt, res: dict) -> None:
    """after_hold: an errand is held because its site is signed out (called every time it comes round; written
    once per hold). The hold goes into the history; the policy names the helper (signedin.py and relogin.py already ran
    it: this file never signs in and never types a code). When the helper stopped at a code or has no saved login, the
    failure is "access needed" and signedin's own one ask IS the one prepared request: it is recorded as asked, so
    nothing else asks. Nothing without the switch."""
    if not on(life):
        return
    try:
        s = _state(opp)
        if (s.get('waiting') or {}).get('site') == site:
            return
        t = _dt(now_dt)
        stop = _relogin_stop(life, site, t)
        failure = 'access_needed' if stop else 'signin_expired'
        step = next_step(con, opp, failure, t, key=key, life=life)
        note(con, key, 'signin', f'wait for the {name} sign-in', ACCESS_STOPS.get(stop, 'the account page showed a sign-in page'), 'on hold until it is signed in', t, opp=opp)
        s = _state(opp)
        s.update(failure=failure, since=_iso(t), site=site, waiting={'for': 'signin', 'site': site, 'name': name, 'since': _iso(t)},
                 problem=f'My {name} sign-in has lapsed, so this is on hold.', then=THEN[failure],
                 needed=(NEEDED['access_needed'].format(name=name, why=ACCESS_STOPS[stop]) if stop else NEEDED['signin_expired'].format(name=name)))
        s.setdefault('asked', {})[identity(opp, key, failure)] = {'at': _iso(t), 'by': 'the sign-in ask', 'do': step['do']}
        _set(con, key, 'recovery', s, opp)
        res['recover'] = res.get('recover', 0) + 1
    except Exception as e:
        res.setdefault('errors', []).append(f'recover {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')


def signin_back(con, life, key, opp: dict, now_dt, res: dict) -> None:
    """after_hold: a held errand was let go because the site is signed in again: the SAME item goes on, and the history
    says so. Written once. Nothing without the switch."""
    if not on(life):
        return
    try:
        s = _state(opp)
        if not s.get('waiting'):
            return
        t = _dt(now_dt)
        name = (s.get('waiting') or {}).get('name') or s.get('site') or 'store'
        note(con, key, 'signin', f'pick-up after the {name} sign-in', 'signed in again', 'signed in again: it went on where it left off', t, opp=opp)
        s = _state(opp)
        for a in (s.get('asked') or {}).values():
            a.setdefault('answered', _iso(t))
        s.update(failure=None, resumed=(list(s.get('resumed') or []) + [{'at': _iso(t), 'why': 'signed in again'}])[-5:])
        for k in ('waiting', 'problem', 'needed', 'then', 'since', 'site'):
            s.pop(k, None)
        _set(con, key, 'recovery', s, opp)
        res['recover_resumed'] = res.get('recover_resumed', 0) + 1
    except Exception as e:
        res.setdefault('errors', []).append(f'recover {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')


# ---- lifecli.py: the owner supplied the fact; the owner took a permission back ---------------------------------------------------------

def _back_to_work(con, key, p: dict, state: str, why: str, t: datetime, **more) -> bool:
    """The SAME item goes back to be worked, once: its unfinished result is put aside (kept, like a correction keeps
    it), and the normal start picks it up on the next run. One guarded write."""
    aside = {'at': _iso(t), 'state': state, 'why': why}
    for k in ('packet', 'told', 'ready_at', 'asked_later', 'held_overnight', 'exec', 'attention', 'signin_not_renewed'):
        if k in p:
            v = p.pop(k)
            if k in ('packet', 'told'):
                aside[k] = v
    p['superseded'] = (list(p.get('superseded') or []) + [aside])[-5:]
    s = _state(p)
    for a in (s.get('asked') or {}).values():
        a.setdefault('answered', _iso(t))
    s.update(failure=None, resumed=(list(s.get('resumed') or []) + [{'at': _iso(t), 'why': why}])[-5:], **more)
    for k in ('waiting', 'problem', 'needed', 'then', 'since', 'site'):
        s.pop(k, None)
    p['recovery'] = s
    if isinstance(p.get('decision'), dict):
        p['decision']['surface_at'] = _iso(t)
    cur = con.execute('UPDATE opps SET state=?, surface_at=?, payload=?, updated=? WHERE key=? AND state=?',
                      ('pending', _iso(t), json.dumps(p, default=str), time.time(), str(key), state))
    return cur.rowcount == 1


def supplied(con, life, key, words: str, now=None) -> bool:
    """`life note <item> "<what the owner said>"`: when this item is waiting on a fact only the owner could give, the owner's answer sends the
    SAME item back to work, once, and the next run is handed the owner's words. An item that is not waiting on the owner, one that
    was already sent back for this ask, and everything without the switch: nothing. -> True when it went back."""
    if not on(life):
        return False
    try:
        row = _item(con, key)
        if row is None or row[0] not in ('ready', 'surfaced', 'needs_attention'):
            return False
        state, p = row[0], row[1]
        s = _state(p)
        blocked = (p.get('packet') or {}).get('status') == 'blocked'
        if s.get('failure') not in ('missing_fact', 'access_needed') and not blocked:
            return False
        t = _dt(now)
        if not _back_to_work(con, key, p, state, 'the owner supplied what was missing', t, supplied={'at': _iso(t), 'words': _one(words, 400)}):
            return False
        con.commit()
        note(con, key, 'worker', 'background run', 'the owner supplied what was missing', 'sent back to work with their answer', t)
        con.commit()
        return True
    except Exception:
        return False


def _take_back(con, life, key, state, p: dict, t: datetime, out: dict) -> None:
    """One open item loses its standing OK: its steps wait for the owner's yes again, and a step of it that was marked as about
    to be carried out under the standing OK, with nothing sent yet, is cancelled. A run already under way cannot be
    recalled from here: that is written into the history, and whatever it did is read back when it ends."""
    dec = p.get('decision') if isinstance(p.get('decision'), dict) else {}
    steps = [x for x in dec.get('steps') or [] if isinstance(x, dict) and x.get('granted')]
    if not steps:
        return
    if state == 'executing':
        note(con, key, 'worker', 'background run', 'the owner took the standing OK back while the run was under way',
             'it cannot be recalled; what it did is read back when it ends', t)
        out['running'] = out.get('running', 0) + 1
        return
    for x in steps:
        x.pop('granted', None)
        x['autonomy'] = 'ask'
    dec['granted'] = False
    dec.pop('granted_by', None)
    if dec.get('autonomy') == 'auto':
        dec['autonomy'] = 'ask'
    s = _state(p)
    s['stopped'] = {'at': _iso(t), 'why': 'the owner took the standing OK back', 'do': POLICY['stopped']['do']}
    p['recovery'] = s
    con.execute('UPDATE opps SET payload=?, updated=? WHERE key=? AND state=?', (json.dumps(p, default=str), time.time(), key, state))
    try:
        import actions as A
        if A.on(life):
            A.ready(con)
            rec = A.latest(con, key, 'granted_step')
            if rec is not None and rec['state'] in ('prepared', 'asked', 'approved'):
                A.move(con, rec['id'], 'declined', 'the owner took the standing OK back')
    except Exception:
        pass
    out['stopped'] = out.get('stopped', 0) + 1


def revoked(con, life, family: str, now=None) -> dict:
    """`life ungrant <kind>`: the owner took a standing OK back. Policy: stop the affected work and cancel its future actions.
    Every open item that standing OK covered (matched the way policy.decide matched it: by the item's own dedupe key)
    and that has not started is put back to "needs the owner's yes each time" (_take_back). -> counts. {} without the switch."""
    if not on(life):
        return {}
    out = {}
    try:
        t = _dt(now)
        import policy as P
        for key, state, raw in [tuple(r) for r in con.execute("SELECT key,state,payload FROM opps WHERE state IN ('pending','executing','ready')").fetchall()]:
            p = json.loads(raw)
            fam = P.family(p.get('dedupe_key') or key)
            if family and (fam == family or fam.startswith(family + ':')):
                _take_back(con, life, key, state, p, t, out)
        con.commit()
    except Exception as e:
        out.setdefault('errors', []).append(f'{type(e).__name__}: {str(e)[:120]}')
    return out


# ---- tick.py: once a run, before the work --------------------------------------------------------------------------------

WORK_FAILURES = ('job_interrupted', 'job_never_started', 'missing_fact', 'budget_exhausted')


def _watching(life, wid) -> bool:
    """Is this watch still being read? Not being able to tell counts as yes: a failure is never cleared on a guess."""
    try:
        import watches as W
        d = W.Store(life).load()
        if d is None:
            return True
        w = W._find(d, wid)
        return w is not None and w.get('state') in W.ACTIVE
    except Exception:
        return True


def tend(con, life, now_dt) -> dict:
    """The tick's step, right before the work (switch recover-on). Sends nothing and starts nothing by itself.
      resumed     an item that gave up waiting for a sign-in (told without the prep, still open) goes back to work, once,
                  when a read shows that site signed in again
      stopped     a standing OK that is no longer in grants.json: open items that still carried it wait for the owner's yes
      unknown     a step that may have gone and that code cannot read back (anything but an email reply): its item gets
                  the open failure and the one ask to look themselves
      escalated   every open failure without its ask yet: escalate() by the two-hour rule; an ask that was due in the
                  morning comes forward, marked urgent, when the deadline comes inside two hours
    Never raises: trouble is listed under 'errors'."""
    if not on(life):
        return {}
    out = {}
    t = _dt(now_dt)
    life = Path(life)

    def guard(name, fn):
        try:
            fn()
        except Exception as e:                               # no rollback: earlier steps of the tick may hold unsaved writes of their own on this connection
            out.setdefault('errors', []).append(f'{name}: {type(e).__name__}: {str(e)[:120]}')

    def resume_signins():
        import signedin as S
        d = S._store(life).load() or {}
        for key, state, raw in [tuple(r) for r in con.execute("SELECT key,state,payload FROM opps WHERE state IN ('ready','surfaced','needs_attention') "
                                                              "AND json_extract(payload,'$.recovery.waiting.for')='signin'").fetchall()]:
            p = json.loads(raw)
            site = (_state(p).get('waiting') or {}).get('site')
            now_state = (d.get('sites') or {}).get(site) or {}
            if now_state.get('status') != 'signed_in' or now_state.get('lapse'):
                continue
            name = (_state(p).get('waiting') or {}).get('name') or site
            if _back_to_work(con, key, p, state, 'signed in again', t):
                con.commit()
                note(con, key, 'signin', f'pick-up after the {name} sign-in', 'signed in again', 'signed in again: sent back to work', t)
                out['resumed'] = out.get('resumed', 0) + 1
        con.commit()

    def gone_grants():
        import policy as P
        grants = P.Grants(life / 'grants.json')
        got = {}
        for key, raw in [tuple(r) for r in con.execute("SELECT key,payload FROM opps WHERE state='pending' AND json_extract(payload,'$.decision.granted')").fetchall()]:
            p = json.loads(raw)
            if not grants.granted(p.get('dedupe_key') or key):      # the standing OK this item was marked under is no longer in grants.json
                _take_back(con, life, key, 'pending', p, t, got)
        con.commit()
        if got.get('stopped'):
            out['stopped'] = out.get('stopped', 0) + got['stopped']

    def unknown_steps():
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='actions'").fetchone() is None:
            return
        for rec_id, item, kind, details in [tuple(r) for r in con.execute("SELECT id,item,kind,details FROM actions WHERE state='unknown' AND kind!='email_reply'").fetchall()]:
            row = _item(con, item)
            if row is None or row[0] not in OPEN or _state(row[1]).get('failure'):
                continue
            try:
                d = json.loads(details) if details else {}
            except ValueError:
                d = {}
            what = _one(d.get('ask') or d.get('words') or row[1].get('title'), 160)
            next_step(con, row[1], 'uncertain_effect', t, key=item, life=life)
            open_failure(con, life, item, 'uncertain_effect', t, ticket=False, problem=f'A step you approved may or may not have been carried out: {what}',
                         needed=NEEDED['uncertain_effect'].format(where=what))
            out['unknown_steps'] = out.get('unknown_steps', 0) + 1
        con.commit()

    def escalations():
        for key, state, raw in [tuple(r) for r in con.execute("SELECT key,state,payload FROM opps WHERE state IN ('pending','executing','ready','surfaced','needs_attention') "
                                                              "AND json_extract(payload,'$.recovery.failure') IS NOT NULL").fetchall()]:
            p = json.loads(raw)
            s = _state(p)
            if s.get('waiting') or s.get('covered_by'):
                continue                                     # the sign-in ask is signedin.py's own; a covered page needs nothing from the owner
            if s.get('failure') in WORK_FAILURES:
                # a failure of the background work is carried to the owner by the item itself, never by a second item. And once
                # the item is back at work (a correction, the owner's answer, a fresh look) that failure is over.
                if state in ('pending', 'executing') and 'packet' not in p:
                    clear(con, key, 'it went back to work', t)
                continue
            if s.get('arm') and not (((p.get('commitment') or {}).get('next_action') or {}).get('gave_up')):
                clear(con, key, 'the plan for watching it was changed, so the arm step starts again', t)
                continue
            if s.get('watch') and not _watching(life, s['watch']):
                clear(con, key, 'that page is no longer watched', t)
                continue
            if escalate(con, life, key, t):                   # '' when this same ask was already made
                out['escalated'] = out.get('escalated', 0) + 1
        cur = con.execute("UPDATE opps SET urgency='now', surface_at=? WHERE state='ready' AND json_extract(payload,'$.recover.near_ts') IS NOT NULL "
                          "AND json_extract(payload,'$.recover.near_ts')<=? AND (urgency!='now' OR surface_at>?)", (_iso(t), t.timestamp(), _iso(t)))
        if cur.rowcount:
            out['came_forward'] = cur.rowcount
        con.commit()

    for name, fn in (('resume', resume_signins), ('grants', gone_grants), ('unknown', unknown_steps), ('escalate', escalations)):
        guard(name, fn)
    return out
