"""After the Zeus in the owner's chat acts on the owner's yes: read back what that stretch of the conversation really did.

The background jobs have this already (jobaudit.py): their instructions forbid sends and changes, and code reads back
every tool call. On the chat side, when the owner says yes to a prepared step, the chat Zeus does the step with its
real tools; `life approve` records the yes (lifecli.py), and this module reads the turn back from Zeus's own records:

  fine        the owner's yes is on record, the step is marked done, something was sent or changed, and no message
              went out unless the prepared step had one in it
  nothing     marked done, but nothing was sent or changed in that stretch
  beyond      a message went out although the prepared step had no message in it
  no_yes      a prepared step was marked done with sends or changes, and no yes of the owner's is on record
  dropped     the owner's yes was recorded, something was sent or changed, and then the item was closed as not done
  unfinished  the owner's yes was recorded more than 20 minutes ago and the step is still not marked done

Anything but "fine" raises one repair ticket in plain words; the raw tool detail goes to the fixer, not to the owner.
This is a tripwire after the fact. It never sends, changes, blocks or undoes anything.

"no_yes" is only recorded, not ticketed, unless the chat rules tell Zeus to record the owner's yes (`life approve`):
a chat version that does not use that command would make a correct send on a plain "send" look like one. The switch
is the file `chat-records-his-yes` in the life-engine folder, created when the chat version records yeses.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import jobaudit
import settings

UNFINISHED_AFTER = 20 * 60
LOOK_BACK = 10 * 60        # with no recorded yes, the stretch before the close that is read
MESSAGE = re.compile(r"_(?:SEND|REPLY|FORWARD)(?:_|\b)|\bsend_message\b|\bimsg\s+(?:send|reply)\b|\bgog\b.*\bsend\b|\b(?:mail|sendmail|msmtp)\s", re.I)
TITLES = {'nothing': 'Zeus marked a step you approved as done, but nothing was found sent or changed: {t}',
          'beyond': 'Zeus sent a message while doing a step you approved that had no message in it: {t}',
          'no_yes': 'Zeus did a prepared step without your yes on record: {t}',
          'dropped': 'Zeus started a step you approved and then closed it as not done: {t}',
          'unfinished': 'Zeus has not finished a step you approved over 20 minutes ago: {t}',
          'twice': 'Zeus sent a message of its own on top of the reply you approved: {t}'}


def chat_writes(state_db: Path, since: float, until: float):
    """What Zeus sent or changed in its one conversation with the owner in this stretch -> list of details, or None when
    the records cannot be read (never treated as clean)."""
    try:
        con = sqlite3.connect(f'file:{state_db}?mode=ro', uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT id FROM sessions WHERE title='Bot Chat' ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        writes = []
        for (tc,) in con.execute('SELECT tool_calls FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<=? AND tool_calls IS NOT NULL '
                                 'ORDER BY timestamp', (row[0], since, until)):
            try:
                calls = json.loads(tc)
            except ValueError:
                continue
            for c in calls if isinstance(calls, list) else []:
                if not isinstance(c, dict):
                    continue
                fn = c.get('function') or {}
                name = fn.get('name') or c.get('name') or '?'
                argtext = fn.get('arguments') if isinstance(fn.get('arguments'), str) else json.dumps(fn.get('arguments') or c.get('arguments') or {})
                try:
                    kind, detail = jobaudit.classify(name, argtext or '')
                except Exception as e:
                    kind, detail = 'write', f'{name}: could not be checked ({type(e).__name__})'
                if kind == 'write':
                    writes.append(str(detail)[:200])
        return writes
    except sqlite3.Error:
        return None
    finally:
        con.close()


def has_message_step(p: dict) -> bool:
    steps = list(p.get('steps') or []) + list((p.get('decision') or {}).get('steps') or [])
    return any(s.get('action_class') == 'message_outside_person' for s in steps if isinstance(s, dict))


def judge(p: dict, state: str, writes: list, now: float) -> str | None:
    """-> one of the findings above, or None when there is nothing to judge yet."""
    ap, cl = p.get('approved') or {}, p.get('closed') or {}
    by_chat = cl.get('by') == 'zeus chat'
    if ap and not cl:
        return 'unfinished' if now - float(ap.get('at') or now) >= UNFINISHED_AFTER else None
    if ap and by_chat and cl.get('state') != 'done':
        return 'dropped' if writes else 'fine'            # the owner changed their mind, or it could not be done, before anything happened
    if ap:
        if (ap.get('send') or {}).get('state') == 'sent':     # code sent the stored reply itself and kept the receipt
            return 'twice' if any(MESSAGE.search(w) for w in writes) else 'fine'
        if not writes:
            return 'nothing'
        return 'beyond' if any(MESSAGE.search(w) for w in writes) and not has_message_step(p) else 'fine'
    return 'no_yes' if writes else 'fine'                 # closed as done with no yes on record: fine only if Zeus itself did nothing


def records_for(life_home):
    """Where Zeus's own chat records are read from for this life folder, or None when there are none to read here:
    LIFE_CHAT_DB when it is set, else the live store only when this IS the live life folder, so a sandbox or a test
    tick never reaches into the live profile."""
    if os.environ.get('LIFE_CHAT_DB'):
        return Path(os.environ['LIFE_CHAT_DB'])
    try:
        real = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))
        live = Path(life_home).resolve() == real.resolve()
    except Exception:
        live = False
    return Path(jobaudit.STATE) if live else None


def run(con, life_home: Path, *, state_db: Path | None = None, now: float | None = None, alarm=None) -> dict:
    """One pass over items the chat Zeus approved or closed as done since this check was switched on."""
    now = time.time() if now is None else now
    state_db = Path(state_db) if state_db else records_for(life_home)
    if alarm is None:
        import worker as W
        alarm = W._alarm
    life_home = Path(life_home)
    marker = life_home / 'acted-check.json'
    try:
        since = float(json.loads(marker.read_text())['since'])
    except (OSError, ValueError, KeyError, TypeError):
        marker.write_text(json.dumps({'since': now, 'why': 'items closed before this moment are history and are not read back'}))
        return {}
    out = {}
    rows = con.execute("SELECT key,state,payload FROM opps WHERE json_extract(payload,'$.approved.at') IS NOT NULL "
                       "OR (state='done' AND json_extract(payload,'$.closed.by')='zeus chat')").fetchall()
    for r in rows:
        p = json.loads(r['payload'])
        ap, cl = p.get('approved') or {}, p.get('closed') or {}
        decision = (p.get('packet') or {}).get('decision') or {}
        started = float(ap.get('at') or 0) or (float(cl.get('at') or 0) - LOOK_BACK)
        if p.get('acted_check') or max(float(ap.get('at') or 0), float(cl.get('at') or 0)) < since:
            continue
        if not ap and not (decision.get('ask') or decision.get('ready')):
            continue                                      # nothing was prepared for the owner's yes on this one: nothing to read back
        if ap and not cl and p.get('unfinished_alarmed'):
            continue
        writes = chat_writes(state_db, started - 5, float(cl.get('at') or now) + 5) if state_db is not None else None   # no records to read here: unreadable, never "clean"
        if writes is None:
            out['unreadable'] = out.get('unreadable', 0) + 1
            continue
        finding = judge(p, r['state'], writes, now)
        if finding is None:
            continue
        out['checked'] = out.get('checked', 0) + 1
        quiet = finding == 'no_yes' and not (life_home / 'chat-records-his-yes').exists()
        if finding != 'fine' and not quiet:
            out['flagged'] = out.get('flagged', 0) + 1
            alarm(life_home, r['key'], TITLES[finding].format(t=p.get('title')), kind='life_acted_' + finding,
                  detail=[f'finding: {finding}', f"the owner's words: {ap.get('owners_words') or '(none recorded)'}"] + writes[:4])
        if finding == 'unfinished':
            p['unfinished_alarmed'] = now                 # read back again when it is finally closed
        else:
            p['acted_check'] = {'at': now, 'finding': finding, 'sent_or_changed': len(writes), 'what': writes[:5]} | (
                {'ticket': "none: the chat rules do not record the owner's yes yet"} if quiet else {})
        con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(p, default=str), r['key']))
    con.commit()
    return out
