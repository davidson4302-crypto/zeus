"""life watch / life signin: how Zeus, in the owner's chat, sets up and stops watches, and looks at sign-ins.

lifecli.py hands these two verbs here (one small hook in its main). Each answers None when its switch file is
absent, and lifecli then goes on exactly as it did before.

  life watch add "<what you are waiting to see>" --url <address> [--chrome]
        --line "<a label on the line that matters>"
      | --pattern "<fixed words with * for the part that changes>"
      | --present "<words that will appear or disappear>" --page-has "<other words always on that page>"
        [--page-has "<words that prove it is the right page>"] [--days N] [--why "<the owner's exact words>"] [--item <item>]
        [--below N | --above N]   only speak when the number on that part is below / above N ("tell me when it is under $300")
                               "tell me when this changes": code reads that page now, and from then on re-reads it
                               on a timer and tells your pipes when that part is different. --chrome reads it in
                               your own signed-in Chrome (only the owner's signed-in sites); without it the page is read
                               as a public page. Nothing is saved unless the page could be read and the part found.
  life watch list [--all]      what is being watched, what each last said, when it is looked at next
  life watch stop <watch> [why]   the owner no longer wants it (a watch id, or a few words of what it is for)
  life signin                  what each of the owner's signed-in sites last showed
  life signin check <site>     the owner says they have signed in again: look now instead of waiting for the timer

A watch only ever opens and reads an address. It never clicks, types, signs in or sends anything.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import watches as W

FLAGS = ('--url', '--line', '--pattern', '--present', '--page-has', '--days', '--why', '--item',
         '--every', '--until', '--fast-from', '--fast-every', '--trust', '--done', '--key',   # the last seven: monitor.py
         '--block', '--block-lines',   # watches.py's block part (switch watches-on): the lines below a name; refused with the reason without the switch
         '--starts',                   # commit.py (switches commit-on + watches-on): save a monitor to begin later; refused with the reason without them
         '--below', '--above')         # speak only when the number on that part is below / above this
MANUAL_GAP = 120      # seconds between two "look now" reads of the same site


def _when(value) -> str:
    t = W._dt(value)
    return t.strftime('%a %b %d at %I:%M%p').replace(' 0', ' ') if t else 'an unknown time'


def _his_words(words: str):
    """The owner's real message that contains these words (last 48 hours), or None. Same check as `life grant`."""
    import chats as C
    msgs = C.messages(time.time() - 48 * 3600, path=os.environ.get('LIFE_CHAT_DB') or None)
    return C.quoted_in(words, msgs)


def _describe(w: dict) -> str:
    part = w.get('part') or {}
    what = f"line \"{part['line']}\"" if part.get('line') else f"pattern \"{part['pattern']}\"" if part.get('pattern') else f"whether \"{part.get('present')}\" is on the page"
    state = {'watching': 'watching', 'stopped': 'stopped', 'ended': 'ended'}.get(w.get('state'), str(w.get('state')))
    bits = [f"[{w.get('id')}] {w.get('for')}  ({state})", f"   reads: {w['read']['url']}" + (' in your Chrome' if w['read']['kind'] == 'chrome' else '') + f", {what}",
            f"   last said: {w.get('value')} (since {_when(w.get('value_at'))})"]
    if w.get('state') == 'watching':
        bits.append(f"   next look: {_when(w.get('next_check'))}; until {_when(w.get('ends_at'))}")
        if w.get('fails'):
            bits.append(f"   could not be read the last {w['fails']} time(s): {w.get('unknown_why')}. That is unknown, not a change.")
    elif w.get('closed'):
        bits.append(f"   {state} {_when(w['closed'].get('at'))}: {w['closed'].get('why')}")
    return '\n'.join(bits)


def _until(value, now_dt):
    """--until / --fast-from: an ISO time, or +90m / +3h from now. Anything else is passed on as given and refused there."""
    import re
    from datetime import timedelta
    v = str(value or '').strip()
    m = re.fullmatch(r'\+(\d+)\s*([mh])', v)
    if m:
        return (now_dt + timedelta(**{{'m': 'minutes', 'h': 'hours'}[m.group(2)]: int(m.group(1))})).isoformat()
    return v or None


def _add(a: list, life: Path, now_dt: datetime) -> int:
    opts, words, i = {}, [], 0
    while i < len(a):
        if a[i] in FLAGS and i + 1 < len(a):
            opts[a[i][2:]] = a[i + 1]
            i += 2
        elif a[i] == '--chrome':
            opts['chrome'] = True
            i += 1
        else:
            words.append(a[i])
            i += 1
    part = {k: opts[k] for k in ('line', 'pattern', 'present') if opts.get(k)}
    if opts.get('page-has'):
        part['page_has'] = opts['page-has']
    if opts.get('block') or opts.get('block-lines'):     # watches.validate checks it, and refuses it without the switch watches-on
        part['block'] = opts.get('block') or ''
        if opts.get('block-lines'):
            part['lines'] = opts['block-lines']
    spec = {'for': ' '.join(words), 'read': {'kind': 'chrome' if opts.get('chrome') else 'web', 'url': opts.get('url') or ''}, 'part': part,
            'days': opts.get('days')}
    if opts.get('below') or opts.get('above'):        # one condition; both given is refused by watches.validate
        spec['when'] = {k: opts[k] for k in ('below', 'above') if opts.get(k)}
    if opts.get('every') or opts.get('until'):      # monitor.py: "keep an eye on it until <time>"
        spec['monitor'] = {'every': opts.get('every'), 'until': _until(opts.get('until'), now_dt), 'fast_from': _until(opts.get('fast-from'), now_dt),
                           'fast_every': opts.get('fast-every'), 'trust': opts.get('trust'), 'done': opts.get('done'), 'key': opts.get('key')}
    item_key = item_title = ''
    if opts.get('item'):
        import lifecli as L
        if (life / 'ledger.sqlite3').exists():
            with closing(sqlite3.connect(life / 'ledger.sqlite3', timeout=15)) as con:
                con.row_factory = sqlite3.Row
                r, _near = L.find(con, opts['item'])
            if r is not None:
                import json
                item_key, item_title = r['key'], str(json.loads(r['payload']).get('title') or '')
        if not item_key:
            print(f'No single item matches "{opts["item"]}", so nothing was saved. Leave --item out, or name the item exactly.')
            return 1
    said, checked = str(opts.get('why') or '').strip(), False
    message_id = None
    if said:
        try:
            found = _his_words(said)
        except Exception:
            found = None
        if found is not None:
            checked, message_id = True, found.get('id')
    if opts.get('starts'):     # commit.py: "start reading it at <time>", saved as a step code carries out then. None = that time is not in the future: start now, as below
        later = __import__('commit').save_arm_step(life, spec, opts['starts'], item_key, said, now_dt)
        if later is not None:
            print(later[1])
            return later[0]
    rec, why = W.add(life, spec, by='chat', now_dt=now_dt, item=item_key, item_title=item_title, words=said, words_checked=checked,
                     message_id=message_id)
    if rec is None:
        print('Not saved: ' + why + '.' + __import__('commit').starts_hint(life, why))   # '' unless commit-on + watches-on and the end time was more than 48 hours away
        clean, _problems = W.validate(spec, life)
        if clean is not None:            # the address was fine; the page or the part was the trouble: show what is close
            near = W.close_lines(clean['part'], W.read(life, clean['read']))
            if near:
                print('Lines on that page that look close:\n' + '\n'.join('- ' + ln for ln in near))
        print('Nothing is being watched for this. Do not tell the owner you are watching it.')
        return 2
    if why == 'already':
        m = rec.get('monitor')
        extra = f" Monitoring every {m.get('fast_every') or m.get('every')} min until {_when(m.get('until'))}." if m else ''
        print(f"That is already being watched: [{rec['id']}] {rec.get('for')}. It last said: {rec.get('value')}.{extra}")
        return 0
    if item_key:
        __import__('commit').on_watches(None, life, item_key, [rec], now_dt)   # commit.py (switch commit-on): the item's commitment lists this watch; nothing without the switch
    if rec.get('monitor'):
        m = rec['monitor']
        print(f"Monitoring. Right now it says: \"{rec['value']}\"\n"
              f"Code reads it every {m['every']} min" + (f" (every {m['fast_every']} min from {_when(m['fast_from'])})" if m.get('fast_from') else '')
              + f" until {_when(m['until'])}, or until it says one of: {', '.join(m['done'])}. "
              + ('This is the operator\'s own page.' if m['trust'] == 'source' else 'This is a tracker: say "the tracker says", never confirmed.')
              + f"\nWatch: {rec['id']}. To stop it: life watch stop {rec['id']}")
        return 0
    if rec.get('when'):                               # a below / above condition
        cond = W.condition_words(rec['when'])
        now_line = (f'It is {cond} already, right now: tell the owner now.' if rec.get('met') else f'It is not {cond} yet.')
        print(f"Watching for {cond}. Right now it says: \"{rec['value']}\". {now_line}\n"
              f"Code looks again in 30 minutes, then less often while nothing changes (at most 4 hours apart), until {_when(rec['ends_at'])}, "
              f"and your pipes hear only when it becomes {cond}.\n"
              f"Watch: {rec['id']}. To stop it: life watch stop {rec['id']}")
        return 0
    print(f"Watching. Right now it says: \"{rec['value']}\"\n"
          f"Code looks again in 30 minutes, then less often while nothing changes (at most 4 hours apart), until {_when(rec['ends_at'])}. "
          'When that part changes, your pipes hear the old and the new value and decide what the owner should be told.\n'
          f"Watch: {rec['id']}. To stop it: life watch stop {rec['id']}"
          + ('' if not said or checked else '\n(Those words were not found in one of the owner\'s messages from the last two days, so they are kept as your note, not as the owner\'s words.)'))
    return 0


def _pick(d: dict, text: str):
    live = [w for w in d['watches'] if w.get('state') in W.ACTIVE]
    t = text.strip().lower()
    exact = [w for w in d['watches'] if str(w.get('id')).lower() == t]
    if exact:
        return exact[0], []
    words = [x for x in t.split() if len(x) > 2]
    hits = [w for w in live if words and all(x in str(w.get('for')).lower() for x in words)]
    return (hits[0], []) if len(hits) == 1 else (None, hits)


def _signin(a: list, life: Path, now_dt: datetime) -> int:
    import signedin as S
    table = S.sites(life)
    state = (S._store(life).load() or {}).get('sites') or {}
    said = {'signed_in': 'signed in', 'signed_out': 'SIGNED OUT', 'unknown': 'could not tell'}
    if len(a) < 2 or a[1] != 'check':
        lines = []
        for site, e in table.items():
            s = state.get(site) or {}
            if not S.usable(e):
                lines.append(f"- {e.get('name') or site}: not set up yet, so it is never checked")
            elif not s:
                lines.append(f"- {e.get('name') or site}: not looked at yet")
            else:
                lines.append(f"- {e.get('name') or site}: {said.get('signed_out' if s.get('lapse') else s.get('status'), s.get('status'))} (looked {_when(s.get('checked_at'))})"
                             + (f", since {_when(s['lapse'].get('since'))}; the owner has been asked once" if s.get('lapse') else '')
                             + (f": {s.get('why')}" if s.get('status') == 'unknown' and s.get('why') else ''))
        print('\n'.join(lines) or 'No signed-in sites are listed.')
        return 0
    want = ' '.join(a[2:]).strip().lower()
    site = next((k for k, e in table.items() if want and (want == k.lower() or want == str(e.get('name') or '').lower()
                                                           or any(want == str(m).lower() for m in e.get('match') or []))), None)
    if site is None:
        print('Which site? One of: ' + ', '.join(str(e.get('name') or k) for k, e in table.items()))
        return 2
    name = table[site].get('name') or site
    if not S.usable(table[site]):
        print(f'{name} is not set up for this check yet, so nothing was looked at.')
        return 1
    last = W._dt((state.get(site) or {}).get('checked_at'))
    if last is not None and (now_dt - last).total_seconds() < MANUAL_GAP:
        print(f"Looked {round((now_dt - last).total_seconds())} seconds ago: {said.get('signed_out' if (state.get(site) or {}).get('lapse') else (state.get(site) or {}).get('status'))}. Give it two minutes before looking again.")
        return 0
    status = S.check(site, life, now_dt, force=True)
    if (life / 'ledger.sqlite3').exists():
        with closing(sqlite3.connect(life / 'ledger.sqlite3', timeout=15)) as con:
            con.row_factory = sqlite3.Row
            S.tend(con, life, now_dt)        # closes the ask at once when the owner is signed back in
    if status == 'signed_in':
        print(f'{name} is signed in. Anything that was waiting on it starts on the next run of your pipes, and the ask to the owner is closed.')
    elif status == 'signed_out':
        print(f'{name} still shows its sign-in page, so it is not signed in in your own Chrome yet. Nothing changed.')
    else:
        print(f"Could not tell: {(S._state(life, site) or {}).get('why') or 'the page could not be read'}. That is unknown, not signed out. Nothing changed.")
    return 0


def _release_test(a: list, life: Path) -> int:
    """life watch release-test "<text>": one marked test text through the code-text lane, at most once (monitor.py)."""
    import monitor as M
    if not (Path(life) / 'watches-on').exists():
        print('Not queued: watches-on is off.')
        return 2
    with closing(sqlite3.connect(Path(life) / 'ledger.sqlite3', timeout=15)) as con:
        con.row_factory = sqlite3.Row
        got = M.release_test(con, life, ' '.join(a), datetime.now(W.PACIFIC))
    print({'queued': 'Queued once. It goes out word for word on the next run (9am to 9pm only).',
           'already': 'Already queued or sent once. Nothing new was queued.'}.get(got, got))
    return 0 if got in ('queued', 'already') else 2


def main(a: list, life) -> int | None:
    """a = the command words ('watch', 'add', ...). -> an exit code, or None when the verb's switch file is absent."""
    import signedin as S
    life = Path(life)
    now_dt = datetime.now(W.PACIFIC)
    if a and a[0] == 'signin':
        return _signin(a, life, now_dt) if S.on(life) else None
    if not a or a[0] != 'watch' or not W.on(life):
        return None
    sub = a[1] if len(a) > 1 else 'list'
    if sub == 'add':
        return _add(a[2:], life, now_dt)
    if sub == 'release-test':
        return _release_test(a[2:], life)
    d = W.Store(life).load()
    if d is None:
        print('The list of watches could not be read. Nothing was changed. Tell the developer.')
        return 1
    if sub == 'list':
        rows = d['watches'] if '--all' in a else [w for w in d['watches'] if w.get('state') in W.ACTIVE]
        print('\n'.join(_describe(w) for w in rows) or 'Nothing is being watched.')
        return 0
    if sub == 'stop' and len(a) > 2:
        w, near = _pick(d, a[2])
        if w is None:
            print(f'No single watch matches "{a[2]}".' + (' Close matches:\n' + '\n'.join(_describe(x) for x in near[:5]) if near else ' See: life watch list'))
            return 1
        if not W.stop(life, w['id'], ' '.join(a[3:]).strip() or 'stopped in chat', now_dt):
            print(f"That one is not being watched any more ({w.get('state')}).")
            return 0
        __import__('commit').on_watch_stop(life, w, now_dt)   # commit.py (switch commit-on): the job it belonged to no longer lists it; nothing without the switch
        print(f"Stopped watching: {w.get('for')}")
        return 0
    print(__doc__)
    return 2
