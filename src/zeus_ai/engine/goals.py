"""goals: personal goals the owner gives Zeus, each with its own page and history.

The feature is silent until the owner gives Zeus a goal: with no goal on record it creates no table, adds nothing to
the research files and wakes no model.

What a goal is here:
  - a durable milestone with a title, a description, a status, a current-state line and a dated history of progress
    entries (the owner's words: the owner's own sentence, the day it was said, why it matters and what counts as done
    when the owner said so)
  - its own page (`life goal show <goal>`: code writes it from the record, the chat model only relays it)
  - goals.md in the hourly research files, one entry per goal
  - "a goal by itself does not run anything": no job, no timer and no scheduled text belongs to a goal
Left out on purpose: goal briefings and letters on a system schedule (no digests), per-goal folders and scheduled
jobs, subgoals (steps do that work), and quietly inventing a goal the owner only "implied". Goals can be paused.

The rules this file holds in code:
  1. A goal exists only because the owner said it. Every write from chat carries --said "<the owner's exact words>", and
     lifecli checks those words against a real message of the owner's from the last two days (the rule `life zone` and
     `life grant` follow). A statement the owner made outside chat is added with a `said:<where>:<when>` label
     (claims.fields), never by the chat model. Zeus may ASK once whether something the owner keeps coming back to is a
     goal: only on two messages of the owner's own on two different days, only once per topic ever, one question open at
     a time, and only after the owner has given Zeus a goal personally (so with no goal on record nothing about goals
     can reach the owner). The question lives on the item that carried it; it becomes a goal row only on the owner's
     answer (`life goal keep`, or `drop` for the owner's no).
  2. Code holds the record and every change of state. A model writes words and proposes a next step. A step closes only
     on the owner's word (the owner's quoted message) or on evidence (an exact quote from a source in that run's index).
     A model's "done" closes nothing. A goal closes only on the owner's word.
  3. No digest and no nagging. A goal reaches the owner three ways only: one prepared next step through the existing
     research -> accept -> select -> deliver path, one line in the weekly "week ahead" plan, and an answer when the owner
     asks. One goal is one topic for good (no 21-day window): it is raised again only when the owner wrote about it since,
     a new source supports it, or the record itself changed (the same rule interests follow). A date drawing nearer is
     not new (research.md).
  4. Corrections stick. Dropped or "that's not a goal" keeps the row as its own tombstone with the owner's words and the
     date (each store sets its own line aside): it is not saved again from an older message, Zeus never asks about it
     again, and anything queued about it is closed unsent. Paused: nothing is prepared and queued texts stop.

Storage: two tables in the existing ledger (goals, goal_events), made on the first write. What Zeus prepared and what
became of it is NOT stored twice: the page reads it from research_items and the item's ledger row.

Pattern used: the existing prompt chain with code gates (no new model call, no new job, no new switch; the commands
answer only while research-flow-on is there). Not used: a goal as one commitment row (the opps table is the worker's
queue, and the stages built for one-step errands would work, nag, stale or close a row that lives for months), and an
orchestrator with per-goal jobs and briefings (scheduled texts).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

OPEN = ('active', 'paused')
STATUSES = OPEN + ('done', 'dropped')
COLS = 'id,title,status,owners_words,said_ref,said_at,why,target,done_when,steps,rev,created,updated,closed'
TABLES = (
    'CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, owners_words TEXT NOT NULL, said_ref TEXT NOT NULL, '
    'said_at REAL NOT NULL, why TEXT, target TEXT, done_when TEXT, steps TEXT NOT NULL, rev INTEGER NOT NULL, created REAL, updated REAL, closed TEXT)',
    'CREATE TABLE IF NOT EXISTS goal_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT NOT NULL, at REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, text TEXT, ref TEXT)')
DROPPED_SHOWN_DAYS = 30        # how long a goal the owner dropped stays in front of research as "do not raise"
ASK_OPEN_DAYS = 21             # one unanswered "want me to keep this as a goal?" at a time, for this long
EVIDENCE_KINDS = ('email', 'calendar', 'watch')       # sources whose own text can prove a step (a chat line of the owner's is the owner's word)
WRITE_VERBS = ('add', 'step', 'pause', 'resume', 'drop', 'not', 'done', 'date', 'rename', 'why', 'done-when', 'keep')
HELP = '''life goal: the goals the owner gave you. Code keeps the record; you relay it and never add to it from your own reading.
  life goal list [--all]                 the owner's goals, one line each (reads only)
  life goal show <goal>                  one goal's page: the owner's words, steps and dated history (reads only)
  life goal add "<short title>" --said "<the owner's exact words>" [--by YYYY-MM-DD] [--why "<the owner's words>"] [--done-when "<the owner's words>"]
  life goal step <goal> add "<the step>" --said "<the owner's exact words>"
  life goal step <goal> done <number> --said "<the owner's exact words>"      (skip <number>: the owner does not want that step)
  life goal pause|resume|drop|done <goal> --said "<the owner's exact words>"
  life goal not <goal> --said "<the owner's exact words>"                     the owner says it is not a goal: gone for good
  life goal date <goal> <YYYY-MM-DD | none> --said "<the owner's exact words>"
  life goal rename <goal> "<new title>" --said "<the owner's exact words>"
  life goal keep <goal> --said "<the owner's exact words>"                    the owner's yes to your one question "keep this as a goal?"
<goal> is its id, the start of its id, or words from its title. Every change needs --said: words from a real message of the owner's
in the last two days. Nothing here sends anything.'''


# ---- the record ------------------------------------------------------------------------------------------------------------

def exists(con) -> bool:
    """False until the owner has given a goal: nothing below reads or makes a table before that."""
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone())


def _schema(con) -> None:
    for sql in TABLES:
        con.execute(sql)


def _goal(row) -> dict:
    g = dict(zip(COLS.split(','), tuple(row)))
    g['steps'] = json.loads(g['steps'] or '[]')
    g['closed'] = json.loads(g['closed']) if g['closed'] else None
    return g


def all_goals(con, statuses=None) -> list:
    if not exists(con):
        return []
    return [g for g in map(_goal, con.execute(f'SELECT {COLS} FROM goals ORDER BY created, id').fetchall()) if statuses is None or g['status'] in statuses]


def get(con, gid: str):
    row = con.execute(f'SELECT {COLS} FROM goals WHERE id=?', (gid,)).fetchone() if exists(con) else None
    return _goal(row) if row else None


def _save(con, g: dict, now: float, owner: bool = True) -> None:
    """owner=True: the owner said something or a checked source proved something. Only that moves `rev` and `updated`, which is
    what "something new" means for this goal. Zeus's own bookkeeping (a step it suggested) never does."""
    if owner:
        g['rev'], g['updated'] = int(g['rev']) + 1, now
    con.execute('UPDATE goals SET title=?,status=?,owners_words=?,said_ref=?,said_at=?,why=?,target=?,done_when=?,steps=?,rev=?,updated=?,closed=? WHERE id=?',
                (g['title'], g['status'], g['owners_words'], g['said_ref'], g['said_at'], g['why'], g['target'], g['done_when'], json.dumps(g['steps'], ensure_ascii=False),
                 g['rev'], g['updated'], json.dumps(g['closed'], ensure_ascii=False) if g['closed'] else None, g['id']))


def _event(con, gid: str, actor: str, kind: str, text: str = '', ref: str = '', at: float | None = None) -> None:
    con.execute('INSERT INTO goal_events(goal,at,actor,kind,text,ref) VALUES(?,?,?,?,?,?)', (gid, float(at or time.time()), actor, kind, str(text or '')[:600], str(ref or '')))


def _flat(s) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').lower()).strip()


def slug(title: str) -> str:
    return '-'.join(_flat(title).split())[:40].strip('-')


def fp(g: dict) -> str:
    return f"{g['rev']}:{g['status']}"


def find(con, text: str):
    """-> (the one goal these words name, []) or (None, the near ones). An id, the start of an id, or every word in the title."""
    goals, t = all_goals(con), ' '.join(str(text or '').split())
    if not t:
        return None, []
    hit = [g for g in goals if g['id'] == t] or [g for g in goals if g['id'].startswith(slug(t)) and slug(t)]
    if not hit:
        want = set(_flat(t).split()) - {'goal', 'the', 'my', 'his', 'her', 'their'}
        hit = [g for g in goals if want and want <= set(_flat(g['title']).split())]
    live = [g for g in hit if g['status'] != 'dropped'] or hit
    return (live[0], []) if len(live) == 1 else (None, live)


def _target(value: str, now: float) -> str:
    """'' / 'none' -> ''; an ISO day that has not passed -> that day; anything else raises ValueError."""
    v = str(value or '').strip().lower()
    if v in ('', 'none', 'no date'):
        return ''
    d = date.fromisoformat(v)
    if d < datetime.fromtimestamp(now).date():
        raise ValueError('that day has passed')
    return d.isoformat()


def add(con, title: str, words: str, said_ref: str, said_at: float, *, why: str = '', target: str = '', done_when: str = '', now: float | None = None, kind: str = ''):
    """The owner stated a goal. -> ('saved' | 'restated' | 'exists', goal) or ('refused', why in plain words).
    The caller has already checked that `words` are the owner's (or carries a builder's `said:` label)."""
    now = float(now or time.time())
    title, words = ' '.join(str(title or '').split())[:80], ' '.join(str(words or '').split())[:600]
    gid = slug(title)
    if len(gid) < 3 or len(words) < 4:
        return 'refused', 'A goal needs a short title and the owner\'s own words.'
    try:
        target = _target(target, now)
    except ValueError as e:
        return 'refused', f'The target date could not be used ({e}). Give it as YYYY-MM-DD, today or later, or leave it out.'
    _schema(con)
    old = get(con, gid) or next((g for g in all_goals(con) if g['said_ref'] == said_ref and g['status'] == 'dropped'), None)      # the same message under another title is still the goal the owner dropped
    if old and old['status'] in OPEN:
        return 'exists', old
    if old and old['status'] == 'done':
        return 'refused', f"\"{old['title']}\" is marked done. If the owner is starting it again: life goal resume {old['id']} --said \"<the owner's exact words>\"."
    if old and old['status'] == 'dropped' and float(said_at) <= float((old['closed'] or {}).get('at') or 0):
        # the tombstone: the owner's older words do not bring back what the owner dropped after saying them
        return 'refused', (f"The owner dropped \"{old['title']}\" on {_day((old['closed'] or {}).get('at'))}. It is not saved again from something the owner said before that. "
                           'It comes back only when the owner says so in a new message.')
    if old:                                                  # dropped, and the owner has said it again since
        how = 'restated'
        old.update(status='active', owners_words=words, said_ref=said_ref, said_at=float(said_at), closed=None, why=why or old['why'], target=target or old['target'],
                   done_when=done_when or old['done_when'])
        _save(con, old, now)
        _event(con, old['id'], 'owner', how, words, said_ref, now)
        con.commit()
        return how, old
    con.execute(f"INSERT INTO goals({COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, title, 'active', words, said_ref, float(said_at), why or '', target, done_when or '', '[]', 1, now, now, None))
    _event(con, gid, 'owner', kind or ('relayed' if said_ref.startswith('said:') else 'stated'), words, said_ref, now)
    con.commit()
    return 'saved', get(con, gid)


def withdraw(con, gid: str, now: float, why: str) -> int:
    """Anything prepared or queued about this goal is closed unsent (the owner paused, dropped or finished it). -> how many.
    A queued message that also carries other items keeps its ledger row; only this goal's item leaves it."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_items'").fetchone():
        return 0
    n = 0
    for iid, state, opp_key, dedup in con.execute("SELECT id,state,opp_key,dedup_keys FROM research_items WHERE state IN ('prepared','pending')").fetchall():
        if 'goal:' + gid not in json.loads(dedup or '[]'):
            continue
        con.execute("UPDATE research_items SET state='dropped', decision='skip', reason_code='preferences', note=COALESCE(note,'') || ?, updated=? WHERE id=?",
                    (f'\nNot sent: {why}.', now, iid))
        n += 1
        if opp_key and not con.execute("SELECT 1 FROM research_items WHERE opp_key=? AND state='pending'", (opp_key,)).fetchone():
            row = con.execute("SELECT payload FROM opps WHERE key=? AND state='ready'", (opp_key,)).fetchone()
            if row:
                p = json.loads(row[0])
                p['closed'] = {'state': 'declined', 'at': now, 'by': 'goals', 'why': f'not sent: {why}'}
                p['told'] = {'at': now, 'silent': True}
                con.execute("UPDATE opps SET state='declined', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(p, default=str), now, opp_key))
    return n


CHANGES = {      # verb: (statuses it applies to, the new status, the history word)
    'pause': (('active',), 'paused', 'paused'), 'resume': (('paused', 'done', 'dropped'), 'active', 'resumed'),
    'drop': (OPEN + ('done',), 'dropped', 'dropped'), 'not': (OPEN + ('done',), 'dropped', 'not_a_goal'), 'done': (OPEN, 'done', 'done')}


def change(con, g: dict, verb: str, words: str, said_ref: str, said_at: float, now: float | None = None):
    """The owner's word changes the goal's state. -> ('ok', goal, texts withdrawn) or ('refused', why, 0)."""
    now = float(now or time.time())
    frm, to, kind = CHANGES[verb]
    if g['status'] not in frm:
        return 'refused', f"Nothing was changed: \"{g['title']}\" is {WORDS[g['status']]}.", 0
    if g['status'] == 'dropped' and float(said_at) <= float((g['closed'] or {}).get('at') or 0):
        return 'refused', f"Nothing was changed: the owner dropped \"{g['title']}\" after saying that. It comes back only on a new message of the owner's.", 0
    g['status'] = to
    g['closed'] = {'how': kind, 'words': words, 'ref': said_ref, 'at': now} if to in ('dropped', 'done') else None
    _save(con, g, now)
    _event(con, g['id'], 'owner', kind, words, said_ref, now)
    gone = withdraw(con, g['id'], now, {'paused': 'the owner paused this goal', 'dropped': 'the owner dropped this goal', 'done': 'the owner marked this goal done'}[to]) if to != 'active' else 0
    con.commit()
    return 'ok', g, gone


def step_add(con, g: dict, what: str, *, by: str, ref: str = '', words: str = '', item: str = '', now: float | None = None) -> dict:
    """A step on the goal. by='owner': the owner named it (the owner's words checked by the caller). by='zeus': research suggested it with
    a prepared item; it is a suggestion until the owner answers, and a second suggestion replaces one that never reached the owner."""
    now = float(now or time.time())
    what = ' '.join(str(what or '').split())[:200]
    same = next((s for s in g['steps'] if _flat(s['what']) == _flat(what) and s['state'] == 'open'), None)
    if same is None and by == 'zeus':
        told = _told(con)
        same = next((s for s in g['steps'] if s['by'] == 'zeus' and s['state'] == 'open' and s.get('item') and told.get(s['item'], {}).get('state') not in ('surfaced', 'pending')), None)
        if same is not None:
            same['what'] = what
    if same is None:
        same = {'n': max([s['n'] for s in g['steps']] or [0]) + 1, 'what': what, 'by': by, 'state': 'open', 'at': now}
        g['steps'].append(same)
    if item:
        same['item'] = item
    _save(con, g, now, owner=(by == 'owner'))
    if by == 'owner':
        _event(con, g['id'], 'owner', 'step_added', f"The owner added step {same['n']}: {what}" + (f' ("{words}")' if words else ''), ref, now)
    return same


def step_close(con, g: dict, step: dict, state: str, *, how: str, quote: str, ref: str, source: str = '', now: float | None = None) -> None:
    """state 'done' or 'skipped'. how: 'the owner's word' (a quoted message of the owner's) or 'evidence' (a quoted source). Nothing else closes a step."""
    now = float(now or time.time())
    step['state'], step['closed'] = state, {'at': now, 'how': how, 'quote': quote[:300], 'ref': ref, 'source': source[:160]}
    _save(con, g, now)
    _event(con, g['id'], 'owner' if how == 'owner word' else 'code', 'step_' + state,
           f"Step {step['n']} {state} ({step['what']}), " + (f'on the owner\'s word: "{quote[:300]}"' if how == 'owner word' else f'shown by {source[:160] or "a source"}: "{quote[:300]}"'), ref, now)


# ---- what Zeus prepared and what became of it: read from the ledger, never stored twice ---------------------------------------

def _told(con) -> dict:
    """{research item id: {'state', 'at', 'made', 'title', 'goals', 'answer', 'answer_at', 'answer_words', 'key', 'slot', 'ask'}} for items about a goal."""
    out = {}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_items'").fetchone():
        return out
    for iid, state, created, updated, title, dedup, opp_key, okey, slot, ask in con.execute(
            "SELECT id,state,created,updated,title,dedup_keys,opp_key,opportunity_key,run_slot,json_extract(payload,'$.goal_ask') FROM research_items "
            "WHERE dedup_keys LIKE '%\"goal:%' ORDER BY created").fetchall():
        rec = {'key': okey, 'slot': slot, 'ask': json.loads(ask) if ask else None, 'state': state, 'at': float(updated or 0), 'made': float(created or 0), 'title': title or '', 'goals': [k[5:] for k in json.loads(dedup or '[]') if k.startswith('goal:')],
               'answer': '', 'answer_at': 0.0, 'answer_words': ''}
        row = con.execute('SELECT state,payload FROM opps WHERE key=?', (opp_key,)).fetchone() if opp_key and state == 'surfaced' else None
        if row:
            p = json.loads(row[1])
            if isinstance(p.get('approved'), dict):         # `life approve`: the owner's yes, recorded once against the exact step
                rec.update(answer='yes', answer_at=float(p['approved'].get('at') or 0), answer_words=str(p['approved'].get('owners_words') or ''))
            elif row[0] == 'declined' and (p.get('closed') or {}).get('by') == 'zeus chat':      # `life drop` / `life mute` in chat: the owner passed
                rec.update(answer='no', answer_at=float((p.get('closed') or {}).get('at') or 0), answer_words=str((p.get('closed') or {}).get('why') or ''))
        out[iid] = rec
    return out


def asks(con, told: dict | None = None) -> dict:
    """Zeus's one question ("want me to keep this as a goal?") lives on the item that carried it, not in a table of its own.
    -> {goal id: {'id', 'title', 'quotes', 'state', 'at'}} for questions that reached the owner or are queued (a draft that was
    never selected is not a question the owner was asked). A question stops being open once a goal row with its id exists."""
    out = {}
    for t in (_told(con) if told is None else told).values():
        a = t['ask']
        if a and t['state'] in ('surfaced', 'pending') and (a['id'] not in out or t['state'] == 'surfaced'):
            out[a['id']] = dict(a, state=t['state'], at=t['at'])
    return out


def steps_view(con, g: dict, told: dict | None = None) -> list:
    told = _told(con) if told is None else told
    out = []
    for s in g['steps']:
        t = told.get(s.get('item') or '', {})
        out.append(dict(s, sent=t.get('state', ''), sent_at=t.get('at', 0), answer=t.get('answer', ''), answer_at=t.get('answer_at', 0)))
    return out


def next_step(view: list):
    """The step that is next: the first open one, leaving out a suggestion of Zeus's that the owner passed on."""
    return next((s for s in view if s['state'] == 'open' and not (s['by'] == 'zeus' and s['answer'] == 'no')), None)


# ---- words: the page, the list ------------------------------------------------------------------------------------------------

WORDS = {'active': 'active', 'paused': 'paused by the owner', 'done': 'done', 'dropped': 'dropped by the owner'}


def _tz():
    try:
        from ownertime import PACIFIC
        return PACIFIC
    except Exception:
        return None


def _day(ts, tz=None) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz or _tz()).strftime('%a %b %d').replace(' 0', ' ')
    except (TypeError, ValueError, OSError):
        return 'an unknown day'


def _date_words(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime('%a %b %d, %Y').replace(' 0', ' ')
    except (TypeError, ValueError):
        return ''


def _step_line(s: dict, tz=None) -> str:
    who = "the owner's step" if s['by'] == 'owner' else "Zeus's suggestion"
    if s['state'] == 'done':
        c = s.get('closed') or {}
        tail = f"Done {_day(c.get('at'), tz)}, " + (f"on the owner's word: \"{c.get('quote')}\"" if c.get('how') == 'owner word' else f"shown by {c.get('source') or 'a source'}: \"{c.get('quote')}\"")
    elif s['state'] == 'skipped':
        tail = f"Skipped {_day((s.get('closed') or {}).get('at'), tz)}: the owner said \"{(s.get('closed') or {}).get('quote')}\""
    elif s['answer'] == 'yes':
        tail = f"Open. Offered {_day(s['sent_at'], tz)}; the owner said yes {_day(s['answer_at'], tz)}"
    elif s['answer'] == 'no':
        tail = f"The owner passed on it {_day(s['answer_at'], tz)}" if s['by'] == 'zeus' else f"Open. The owner passed on Zeus's offer about it {_day(s['answer_at'], tz)}"
    elif s['sent'] == 'surfaced':
        tail = f"Open. Offered {_day(s['sent_at'], tz)}, no answer yet"
    elif s['by'] == 'zeus':
        tail = 'Open. Prepared, not sent to the owner'
    else:
        tail = 'Open'
    return f"{s['n']}. {s['what']} ({who}). {tail}."


EVENT_WORDS = {'stated': 'The owner gave this goal', 'owners_words': 'The owner\'s own words about it, from before Zeus asked', 'relayed': 'The owner gave this goal (said to a builder, passed on word for word)', 'restated': 'The owner gave this goal again',
               'kept': 'The owner said yes, keep it as a goal', 'answered_no': 'The owner said no to keeping it as a goal', 'asked': 'Zeus asked once whether to keep this as a goal', 'paused': 'The owner paused it', 'resumed': 'The owner resumed it',
               'dropped': 'The owner dropped it', 'not_a_goal': 'The owner said it is not a goal', 'done': 'The owner marked it done', 'target': 'The owner changed the target date',
               'renamed': 'The owner renamed it', 'why': 'The owner said why it matters', 'done_when': 'The owner said what counts as done',
               'step_added': '', 'step_done': '', 'step_skipped': ''}          # a step's line is stored as its own sentence


def history(con, g: dict, told: dict | None = None, tz=None) -> list:
    """Dated lines, oldest first: what the owner said and changed (stored), and what Zeus prepared and what became of it (read from the ledger)."""
    told = _told(con) if told is None else told
    rows = []
    for at, actor, kind, text in con.execute('SELECT at,actor,kind,text FROM goal_events WHERE goal=? ORDER BY seq', (g['id'],)).fetchall():
        words = EVENT_WORDS.get(kind, kind)
        quote = kind in ('stated', 'owners_words', 'relayed', 'restated', 'kept', 'answered_no', 'asked', 'paused', 'resumed', 'dropped', 'not_a_goal', 'done', 'why', 'done_when')
        rows.append((float(at), (words + (f': "{text}"' if quote and text else f': {text}' if text else '')) if words else str(text)))
    for iid, t in told.items():
        if g['id'] not in t['goals']:
            continue
        rows.append((t['made'], f"Zeus prepared: {t['title']}"))
        if t['state'] == 'surfaced':
            rows.append((t['at'], f"Zeus told the owner: {t['title']}"))
            if t['answer']:
                rows.append((t['answer_at'], ('The owner said yes' if t['answer'] == 'yes' else 'The owner passed') + (f": \"{t['answer_words']}\"" if t['answer_words'] else '')))
        elif t['state'] == 'dropped':
            rows.append((t['at'], f"Not sent to the owner: {t['title']}"))
    return [f'{_day(at, tz)}: {line}' for at, line in sorted(rows, key=lambda r: r[0])]


def page(con, g: dict, tz=None) -> str:
    """One goal's page, written by code from the record. Plain words only: it is read to the owner."""
    told = _told(con)
    view = steps_view(con, g, told)
    head = [g['title'], WORDS[g['status']].capitalize() + '.' + (f" Target: {_date_words(g['target'])}." if g['target'] else ' No target date.')]
    said = 'Said to a builder' if g['said_ref'].startswith('said:') else 'The owner said'
    head.append(f"{said} ({_day(g['said_at'], tz)}): \"{g['owners_words']}\"")
    if g['why']:
        head.append(f"Why it matters to the owner: \"{g['why']}\"")
    if g['done_when']:
        head.append(f"Done when: \"{g['done_when']}\"")
    nxt = next_step(view) if g['status'] == 'active' else None
    head.append(f"Next step: {nxt['what']}." if nxt else 'Next step: none set.' if g['status'] == 'active' else '')
    return '\n'.join([x for x in head if x] + ['', 'Steps'] + ([_step_line(s, tz) for s in view] or ['None yet.']) + ['', 'History'] + history(con, g, told, tz))


def listing(con, show_all: bool = False, tz=None) -> str:
    goals = all_goals(con)
    told = _told(con)
    live = [g for g in goals if g['status'] in OPEN]
    waiting = [a for a in asks(con).values() if not get(con, a['id'])]
    if not live and not waiting and not (show_all and goals):
        return ('The owner has not given you a goal' + (' that is still open' if goals else ' yet') + '. When the owner states one in the owner\'s own words: '
                'life goal add "<short title>" --said "<the owner\'s exact words>". Never make one up from the owner\'s mail or notes.')
    out = []
    for g in live:
        nxt = next_step(steps_view(con, g, told)) if g['status'] == 'active' else None
        out.append(f"- {g['title']} [{g['id']}]: {WORDS[g['status']]}" + (f", target {_date_words(g['target'])}" if g['target'] else '')
                   + (f". Next step: {nxt['what']}" if nxt else '') + '.')
    for a in waiting:
        out.append(f"- You asked once whether to keep \"{a['title']}\" [{a['id']}] as a goal; the owner has not answered. The owner's yes: life goal keep {a['id']} --said \"<the owner's words>\". "
                   f"The owner's no: life goal drop {a['id']} --said \"<the owner's words>\". Do not ask again.")
    rest = [g for g in goals if g['status'] in ('done', 'dropped')]
    if show_all:
        out += [f"- {g['title']} [{g['id']}]: {WORDS[g['status']]} {_day((g['closed'] or {}).get('at'), tz)}." for g in rest]
    elif rest:
        out.append(f'{len(rest)} finished or dropped (life goal list --all).')
    return '\n'.join(out)


# ---- hourly research: goals.md, the tracking lines, the accept gates ----------------------------------------------------------

def add_to_bundle(con, ix, tracking_parts: list, now_ts: float, checkpoint_ts: float, when, week_plan: bool = False) -> list:
    """Active goals get a source label and one line in tracking.md; the lines of goals.md (their pages) come back. A goal the owner
    paused or dropped, and a question still waiting for the owner's answer, get one line in tracking.md and no label. With no
    goal on record nothing is added anywhere and no file is written. Reads only. `when`: research's time formatter."""
    goals = all_goals(con)
    if not goals:
        return []                                            # the owner has given no goal: Zeus asks nothing either (see _ask_check)
    told, parts = _told(con), []
    pages = parts                                            # goals.md: the pages of the owner's active goals only
    changed = sum(1 for g in goals if float(g['updated'] or 0) >= checkpoint_ts)
    if changed:
        ix.signals['goals_changed'] = changed
    for g in [g for g in goals if g['status'] == 'active']:
        view = steps_view(con, g, told)
        nxt = next_step(view)
        lab = ix.add('goals.md', 'goal', 'goal:' + g['id'], 'Goal the owner gave you: ' + g['title'], fp(g), 'goal:' + g['id'])
        is_new = float(g['updated'] or 0) >= checkpoint_ts
        tracking_parts.append(f"- [{lab}] Goal the owner gave you | goal:{g['id']} | {g['title']} | target {g['target'] or 'none'} | next step: {nxt['what'] if nxt else 'none set'} | "
                              f"changed since the last run: {'yes' if is_new else 'no'} | page: goals.md")
        parts += [f"## [{lab}] {g['title']}", '', f"active · Updated: {when(g['updated'])} · Changed since checkpoint: {'true' if is_new else 'false'}", '',
                  f"- **Goal id:** {g['id']}", f"- **The owner's words ({when(g['said_at'])}):** {g['owners_words']}"]
        parts += ([f"- **Why it matters to the owner:** {g['why']}"] if g['why'] else []) + ([f"- **Done when (the owner's words):** {g['done_when']}"] if g['done_when'] else [])
        parts += [f"- **Target date:** {g['target'] or 'none set'}", f"- **Next step:** {nxt['what'] if nxt else 'none set'}", '- **Steps:**'] + [f'  {_step_line(s)}' for s in view] + (
            [] if view else ['  None yet.']) + ['- **History:**'] + [f'  {h}' for h in history(con, g, told)] + ['']
    if week_plan and any(g['status'] == 'active' for g in goals):
        parts += ['# For the Week ahead plan', '', 'Give each active goal above one line in the plan (its title, the next step, the target date if the owner set one) and cite its label.', '']
    quiet = [g for g in goals if g['status'] == 'paused']
    gone = [g for g in goals if g['status'] == 'dropped' and float((g['closed'] or {}).get('at') or 0) >= now_ts - DROPPED_SHOWN_DAYS * 86400]
    asked = [a for a in asks(con, told).values() if not get(con, a['id'])]
    if quiet or gone or asked:
        # These go in tracking.md itself, which every run reads: a goal with no label cannot be cited, and research is told why.
        parts = tracking_parts
        parts += [f"- Goal paused by the owner on {when((g['updated']))} (no label: prepare nothing for it until the owner resumes it): {g['title']}" for g in quiet]
        parts += [f"- Goal dropped by the owner on {when((g['closed'] or {}).get('at'))} (never raise it, or anything built on it, and never ask about it again): {g['title']}" for g in gone]
        parts += [f"- You asked once on {when(a['at'])} whether to keep \"{a['title']}\" as a goal. The owner has not answered. Do not ask again." for a in asked]
    return pages


def _ask_check(con, ix, ask: dict, tz, now_ts: float) -> tuple:
    """Zeus's one question, "want me to keep this as a goal?". -> (goal id, title, quotes) or raises ValueError(why)."""
    goals = all_goals(con)
    if not goals:
        raise ValueError('Zeus asks whether something is a goal only after the owner has given Zeus a goal personally')
    title = ' '.join(str(ask.get('title') or '').split())[:80]
    gid = slug(title)
    if len(gid) < 3:
        raise ValueError('the question needs a short title for the goal')
    before = asks(con)
    if get(con, gid) or gid in before:
        raise ValueError('that is already a goal, already asked, or something the owner dropped')
    if any(a['state'] == 'pending' or (a['at'] >= now_ts - ASK_OPEN_DAYS * 86400 and not get(con, a['id'])) for a in before.values()):
        raise ValueError('one question about a goal is already waiting for the owner\'s answer')
    quotes = []
    for q in ask.get('quotes') if isinstance(ask.get('quotes'), list) else []:
        msg = ix.chat_sources.get(str((q or {}).get('source') or '').strip('[]')) if isinstance(q, dict) else None
        text = ' '.join(str((q or {}).get('quote') or '').split()) if isinstance(q, dict) else ''
        if msg and msg.get('who') == 'user' and 4 <= len(text) <= 300 and text in ' '.join(str(msg['text']).split()) and msg['ref'] not in [x['ref'] for x in quotes]:
            quotes.append({'ref': msg['ref'], 'at': float(msg['at']), 'quote': text})
    if len({datetime.fromtimestamp(q['at'], tz).date() for q in quotes}) < 2:
        raise ValueError('the question needs the owner\'s own words about it from two messages on two different days')
    used = {q['ref'] for a in before.values() for q in a['quotes']} | {g['said_ref'] for g in goals if g['status'] == 'dropped'}
    if used & {q['ref'] for q in quotes}:
        raise ValueError('the owner was already asked about those words, or dropped the goal they gave')
    return gid, title, quotes


def accept_check(con, ix, proposal: dict, evidence: list, scaffold: str, pw: dict, key: str, slot: str, now_ts: float, tz=None) -> dict:
    """Code's gate on a research proposal that touches a goal. -> {'ids': [goal ids this item is about], 'why': '' or the
    refusal, 'step': the suggested step, 'ask': None or the checked question, 'waits': '' or what waits for the owner's yes}.
    A proposal that cites no goal and asks nothing comes back untouched ({'ids': [], 'why': ''}); with no goal on record the
    weekly plan is untouched too."""
    out = {'ids': [], 'why': '', 'step': '', 'ask': None, 'waits': ''}
    cited = [str(e.get('ref'))[5:] for e in evidence if e.get('kind') == 'goal']
    ask = proposal.get('goal_ask') if isinstance(proposal.get('goal_ask'), dict) else None
    if scaffold == 'week_ahead':
        # The weekly plan lists goals; it is not "the goal's message". A goal the plan left out is handed to the writer as
        # one more line of the prepared work (a fact for the writer, never a sentence added to the owner's text afterwards).
        active = all_goals(con, ('active',))
        told = _told(con) if active else {}
        for g in active:
            if g['id'] not in cited:
                nxt = next_step(steps_view(con, g, told))
                pw['ready'] += f"\nGoal: {g['title']}" + (f", next step: {nxt['what']}" if nxt else '') + (f", target {_date_words(g['target'])}" if g['target'] else '') + '.'
        return out
    if not cited and not ask:
        return out
    if ask:
        if cited:
            out['why'] = 'a question about a possible goal cannot also be about a goal the owner already gave'
            return out
        try:
            gid, title, quotes = _ask_check(con, ix, ask, tz, now_ts)
        except ValueError as e:
            out['why'] = str(e)
            return out
        out.update(ids=[gid], ask={'id': gid, 'title': title, 'quotes': quotes}, waits=f'Keep "{title}" as one of the owner\'s goals (only on the owner\'s yes).')
        return out
    told = _told(con)
    for gid in cited:
        g = get(con, gid)
        if g is None or g['status'] != 'active':
            out['why'] = f"the goal is {WORDS[g['status']] if g else 'not on record'}, so nothing is prepared for it"
            return out
        waiting = next((t for t in told.values() if gid in t['goals'] and (t['state'] == 'pending' or (t['state'] == 'prepared' and t['key'] != key and t['slot'] == slot))), None)
        if waiting:                                          # one prepared step per goal at a time: two texts about one goal never queue up
            out['why'] = 'a message about this goal is already ' + ('queued for the owner' if waiting['state'] == 'pending' else 'prepared this run')
            return out
    step = ' '.join(str(proposal.get('goal_step') or '').split())[:200]
    if step:
        # `goal_step` is the number of a step already on the goal that this item advances, or a few words for a new one. A step
        # that is done or that the owner skipped gets nothing more; neither does a suggestion of Zeus's that the owner passed on.
        for s in steps_view(con, get(con, cited[0]), told):
            if step in (str(s['n']), f"step {s['n']}") or _flat(s['what']) == _flat(step):
                if s['state'] != 'open' or (s['by'] == 'zeus' and s['answer'] == 'no'):
                    out['why'] = 'that step is already done' if s['state'] == 'done' else 'the owner passed on that step'
                    return out
                step = str(s['n'])
                break
        else:
            if re.fullmatch(r'(step )?\d+', step):            # a number that names no step on the page: the item's title stands in
                step = ''
    out.update(ids=cited, step=step)
    return out


def last_raised(con, ids: list, load):
    """The newest item about any of these goals that reached the owner or was dropped, however long ago (a goal has no 21-day
    window: time passing never brings it back). `load`: research._load."""
    keys = {'goal:' + i for i in ids}
    for r in con.execute("SELECT * FROM research_items WHERE state IN ('surfaced','dropped') AND dedup_keys LIKE '%\"goal:%' ORDER BY updated DESC").fetchall():
        it = load(r)
        if keys & set(it['dedup_keys']):
            return it
    return None


def after_accept(con, check: dict, iid: str, title: str, now_ts: float) -> None:
    """The item was accepted: the suggested step joins the goal (a suggestion, open until the owner's word or evidence). A question
    needs nothing here: it is on the item itself. No commit here: research() commits before the next model call."""
    if check.get('ids') and not check.get('ask'):
        g = get(con, check['ids'][0])
        on = next((s for s in (g or {}).get('steps', []) if str(s['n']) == check.get('step') and s['state'] == 'open'), None)
        if on is not None:                                   # the item advances a step that is already there: the step stays whose it was
            on['item'] = iid
            _save(con, g, now_ts, owner=False)
        elif g:
            step_add(con, g, check.get('step') or title, by='zeus', item=iid, now=now_ts)


def apply_updates(con, updates, ix, now_ts: float) -> dict:
    """Research says a step is done. Code believes only a quote: the owner's own chat message (the owner's word) or the text of a mail,
    calendar entry or watched page in this run's index (evidence). -> {'done': ['goal#n'], 'rejected': [why]} (ids only)."""
    out = {'done': [], 'rejected': []}
    for u in updates if isinstance(updates, list) else []:
        if not isinstance(u, dict):
            out['rejected'].append('not a goal update')
            continue
        row = ix.rows.get(str(u.get('goal') or '').strip('[]')) or {}
        g = get(con, str(row.get('ref') or '')[5:]) if row.get('kind') == 'goal' else None
        lab, quote = str(u.get('evidence') or '').strip('[]'), ' '.join(str(u.get('quote') or '').split())
        src, chat = ix.rows.get(lab) or {}, ix.chat_sources.get(lab)
        step = next((s for s in (g or {}).get('steps', []) if str(s['n']) == str(u.get('step')) and s['state'] == 'open'), None)
        his = bool(chat) and chat.get('who') == 'user'
        text = ' '.join(str(chat['text'] if chat else ix.source_text.get(lab, '')).split())
        why = ('the goal label is not an active goal in this run' if not g or g['status'] != 'active' else 'that step is not an open step of the goal' if step is None
               else 'the evidence is not the owner\'s own message or a mail, calendar or watched-page source of this run' if not (his or src.get('kind') in EVIDENCE_KINDS)
               else 'the quote must be 8 to 300 characters' if not 8 <= len(quote) <= 300 else 'the quote is not in that source' if quote not in text else '')
        if why:
            out['rejected'].append(why)
            continue
        step_close(con, g, step, 'done', how='owner word' if his else 'evidence', quote=quote, ref=str((chat or src).get('ref') or ''), source='' if his else str(src.get('title') or ''), now=now_ts)
        out['done'].append(f"{g['id']}#{step['n']}")
    return out


# ---- the chat command: life goal ... --------------------------------------------------------------------------------------------

def _opt(args: list, flag: str) -> str:
    if flag in args:
        i = args.index(flag)
        value = args[i + 1] if i + 1 < len(args) else ''
        del args[i:i + 2]
        return str(value).strip()
    return ''


def _saved_words(how: str, g: dict) -> str:
    lead = {'saved': 'Goal saved', 'restated': 'Goal saved again (the owner had dropped it and has now said it again)', 'kept': 'Goal saved on the owner\'s yes', 'exists': 'Already one of the owner\'s goals, nothing changed'}[how]
    return (f"{lead}: \"{g['title']}\"" + (f", target {_date_words(g['target'])}" if g['target'] else ', no target date') + f".\nHis words ({_day(g['said_at'])}): \"{g['owners_words']}\"\n"
            + ('' if how == 'exists' else 'Tell the owner once, in one line, that it is saved' + (' with the date as you understood it' if g['target'] else '')
               + ', and that the owner can pause, change or drop it any time. Ask the owner nothing about it now and promise no check-ins: a next step comes only when one is useful, '
                 'and the weekly plan lists it in one line.\n') + f"Its page: life goal show {g['id']}")


def cli(a: list, life, db, owners_message, out=print) -> int:
    """`life goal ...` (lifecli passes its own ledger opener and its check of the owner's real words)."""
    args = list(a)
    verb = args.pop(0) if args else 'list'
    if verb in ('help', '-h', '--help'):
        out(HELP)
        return 0
    with closing(db()) as con:
        if verb in ('list', 'ls'):
            out(listing(con, '--all' in args))
            return 0
        if verb == 'show':
            g, near = find(con, ' '.join(args))
            if g is None:
                out((f'No single goal matches "{" ".join(args)}".' + (' Close: ' + ', '.join(f"{x['title']} [{x['id']}]" for x in near) if near else '')) if args else listing(con))
                return 1 if args else 0
            out('The owner\'s goal, from the record. Tell the owner what the owner asked about it from this, shortened if the owner asked something narrow; add nothing that is not here.\n\n' + page(con, g))
            return 0
        if verb not in WRITE_VERBS:
            out(HELP)
            return 2
        said, why, by, done_when = _opt(args, '--said'), _opt(args, '--why'), _opt(args, '--by'), _opt(args, '--done-when')
        if not said:
            out('Not recorded. Quote what the owner said: add --said "<the owner\'s exact words>". Nothing was changed.')
            return 2
        try:
            msg = owners_message(said)
            extra = {k: (v if v and owners_message(v) is not None else '') for k, v in (('why', why), ('done_when', done_when))}
        except Exception as e:
            out(f'Could not check the owner\'s words against the owner\'s real messages ({type(e).__name__}), so nothing was recorded. Nothing changed.')
            return 1
        if msg is None:
            out('Those words are not in any message the owner sent in the last two days, so nothing was recorded. Quote the owner exactly, or ask the owner to say it.')
            return 3
        ref, at, now = f"chat:{msg['id']}", float(msg['at']), time.time()
        left = [k for k, v in (('why', why), ('done_when', done_when)) if v and not extra[k]]
        note = ''.join(f"\nThe {k.replace('_', ' ')} was left out: those words are not in the owner's messages." for k in left)
        if verb == 'add':
            if not args:
                out('Not recorded. Give the goal a short title: life goal add "<short title>" --said "<the owner\'s exact words>".')
                return 2
            how, res = add(con, args[0], said, ref, at, why=extra['why'], target=by, done_when=extra['done_when'], now=now)
            out(res if how == 'refused' else _saved_words(how, res) + note)
            return 3 if how == 'refused' else 0
        if verb == 'step':
            g, near = find(con, args[0] if args else '')
            sub = args[1] if len(args) > 1 else ''
            if g is None or sub not in ('add', 'done', 'skip') or len(args) < 3:
                out('No single goal matches.' if g is None and args else HELP)
                return 1 if g is None and args else 2
            if g['status'] not in OPEN:
                out(f"Nothing was changed: \"{g['title']}\" is {WORDS[g['status']]}.")
                return 3
            if sub == 'add':
                s = step_add(con, g, ' '.join(args[2:]), by='owner', ref=ref, words=said, now=now)
                con.commit()
                out(f"Step {s['n']} added to \"{g['title']}\": {s['what']}.")
                return 0
            s = next((s for s in g['steps'] if str(s['n']) == args[2] and s['state'] == 'open'), None)
            if s is None:
                out(f"Nothing was changed: \"{g['title']}\" has no open step {args[2]}. Its page: life goal show {g['id']}")
                return 1
            step_close(con, g, s, 'done' if sub == 'done' else 'skipped', how='owner word', quote=said, ref=ref, now=now)
            con.commit()
            out(f"Step {s['n']} of \"{g['title']}\" is {'done' if sub == 'done' else 'skipped'}, on the owner's word.")
            return 0
        g, near = find(con, args[0] if args else '')
        asked = asks(con).get(slug(args[0])) if g is None and args and verb in ('keep', 'drop', 'not') else None
        if asked:                                            # the owner's answer to the one question: the goal goes on record with the words the owner was asked about
            how, g = add(con, asked['title'], ' / '.join(q['quote'] for q in asked['quotes']), ref, at, now=now, kind='owners_words')
            if how == 'refused':
                out(g)
                return 3
            for q in asked['quotes']:
                _event(con, g['id'], 'zeus', 'asked', q['quote'], q['ref'], asked['at'])
            _event(con, g['id'], 'owner', 'kept' if verb == 'keep' else 'answered_no', said, ref, now)
            con.commit()
            if verb == 'keep':
                out(_saved_words('kept', g))
                return 0
        if g is None or verb == 'keep':
            out((f'No single goal matches "{args[0] if args else ""}".' if g is None else f"Nothing was changed: \"{g['title']}\" is already {WORDS[g['status']]}.") + (' Close: ' + ', '.join(f"{x['title']} [{x['id']}]" for x in near) if near else '') + ' Nothing was changed.')
            return 1 if g is None else 3
        if verb in CHANGES:
            how, res, gone = change(con, g, verb, said, ref, at, now)
            if how == 'refused':
                out(res)
                return 3
            out({'pause': 'Paused', 'resume': 'Active again', 'drop': 'Dropped', 'not': 'Removed (the owner said it is not a goal)', 'done': 'Marked done on the owner\'s word'}[verb]
                + f": \"{g['title']}\"." + (f' {gone} prepared message(s) about it will not be sent.' if gone else '')
                + (' It will not come back unless the owner says so in a new message, and you never ask about it again.' if verb in ('drop', 'not') else '')
                + (' Nothing is prepared for it until the owner resumes it.' if verb == 'pause' else ''))
            return 0
        if g['status'] not in OPEN:
            out(f"Nothing was changed: \"{g['title']}\" is {WORDS[g['status']]}.")
            return 3
        value = ' '.join(args[1:]).strip()
        if verb == 'date':
            try:
                g['target'] = _target(value, now)
            except ValueError as e:
                out(f'Nothing was changed: the date could not be used ({e}). Give it as YYYY-MM-DD, today or later, or "none".')
                return 2
            _save(con, g, now)
            _event(con, g['id'], 'owner', 'target', f"{_date_words(g['target']) or 'no date'} | \"{said}\"", ref, now)
        else:
            if len(value) < 3 and verb == 'rename':
                out('Nothing was changed: give the new title.')
                return 2
            field = {'rename': 'title', 'why': 'why', 'done-when': 'done_when'}[verb]
            g[field] = (value[:80] if verb == 'rename' else said)[:600]
            _save(con, g, now)
            _event(con, g['id'], 'owner', {'rename': 'renamed', 'why': 'why', 'done-when': 'done_when'}[verb], g[field] if verb == 'rename' else said, ref, now)
        con.commit()
        out(f"Changed \"{g['title']}\": " + {'date': f"target {_date_words(g['target']) or 'removed'}", 'rename': 'new title', 'why': 'why it matters, in the owner\'s words',
                                           'done-when': 'what counts as done, in the owner\'s words'}[verb] + f". Its page: life goal show {g['id']}")
        return 0


def main(argv=None) -> int:
    """For a builder only: pass on, word for word, a goal the owner stated outside Zeus's chat (the `said:` label).
    python -IB goals.py relay <life folder> "<short title>" --said "<the owner's exact words>" --where <where the owner said it> --when <ISO time> [--by ...] [--why ...] [--done-when ...]
    The life folder is always given: there is no default, so this can never reach a live ledger by accident."""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 3 or a[0] != 'relay':
        print(main.__doc__)
        return 2
    life, title, rest = Path(a[1]), a[2], a[3:]
    said, where, when, by, why, done_when = (_opt(rest, f) for f in ('--said', '--where', '--when', '--by', '--why', '--done-when'))
    try:
        at = datetime.fromisoformat(when).timestamp()
    except ValueError:
        at = 0
    if not said or not re.fullmatch(r'[a-z0-9-]{2,40}', where) or not at or not (life / 'ledger.sqlite3').exists():
        print('Not recorded: give the owner\'s exact words, where the owner said them (letters and dashes), an ISO time with offset, and a life folder that has a ledger.')
        return 2
    with closing(sqlite3.connect(life / 'ledger.sqlite3', timeout=15)) as con:
        how, res = add(con, title, said, f'said:{where}:{when}', at, why=why, target=by, done_when=done_when)
        print(res if how == 'refused' else _saved_words(how, res))
    return 3 if how == 'refused' else 0


if __name__ == '__main__':
    sys.exit(main())
