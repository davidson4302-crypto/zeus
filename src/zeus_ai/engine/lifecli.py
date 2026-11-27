"""life: how Zeus, in the owner's chat, sees and closes the items its own life pipes are working.

One brain: without this tool, when the owner says "stop that reminder", the chat side of Zeus could only answer "a
background check I run every 5 minutes could re-flag that email". With it the same Zeus can list its open items, read
what was prepared, and close them, so "send", "I've got that" and "stop" land in the same list the pipes use.

  life list [--all]            open items (what you told the owner, what is prepared, what is still being worked)
  life show <item>             everything about one item, including the full prepared draft or options
  life approve <item> [the owner's words]   the owner said yes to the prepared step: records the owner's yes, then prints the EXACT prepared
                               step for you to do now. A second approve on the same item is refused (never do it twice).
  life told <item> [note]      you just told the owner about a worked item in chat, so the next note does not repeat it
  life done <item> [note]      finished (you did the step the owner approved, or it is simply done)
  life drop <item> [note]      the owner does not want this one ("I've got that", "ignore for now"). This item only.
  life mute <item> [note]      the owner said stop or never: close it AND never raise this kind of thing again
  life note <item> <text>      remember something the owner said about it ("leaving from the house")
  life add "<what>" [--due <ISO time | +2h | +3d>] [--why "<the owner's words>"]
                               something the owner mentioned in chat that you should prepare, watch or remind the owner of
                               (a visitor, a trip, a deadline, a birthday). Your pipes work it and tell the owner.
  life grant <item> "<the owner's exact words>"
                               the owner said to ALWAYS handle this kind of thing without asking ("from now on just refill it").
                               Records a standing OK for this kind of item only. The owner's words must be a real recent message
                               of the owner's and must say it is standing: a plain "yes" is a one-time approve, never a grant.
  life grants                  the standing OKs the owner has given
  life ungrant <kind or item>  the owner took one back: this kind of thing waits for the owner's yes again
  life rule yes <item> "<the owner's exact words>"
                               the owner said yes to an offer to handle something the same way every time from now on (an item
                               whose key starts "offer:"). The owner's words must be in a real recent message of the owner's. Records it for good.
  life rule no <item> [the owner's words]   the owner said no to that offer. The owner is never asked about it again.
  life rule retire <rule> "<why>"   the owner took a standing way of working back, or it went wrong: it stops applying
                               (also for a habit you picked up by yourself from the owner's mail: see life rules)
  life rules                   the standing ways of working the owner has been offered, and what the owner answered
  life zone                    where the owner is treated as being (which clock the owner's quiet hours follow)
  life zone <city or zone> [until <day>] --said "<the owner's exact words>"
                               the owner said they are away ("I'm in Chicago until Friday"): quiet hours
                               follow the clock there, then go back to home time by themselves. The owner's words must be a real
                               recent message of the owner's that names the place. life zone home: the owner is back.
  life stop                    the owner's plain "stop": all life pipes stop until the owner says resume
  life resume

<item> is a key, the start of a key, or a few words from the title. Reads and writes only its own small ledger.
It never sends anything and never touches email, calendar or money.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prefs as F  # noqa: E402
import settings  # noqa: E402
import re  # noqa: E402

# The owner's words for a standing OK must say it is standing. Without one of these it is a one-time yes (life approve).
STANDING = re.compile(r"\b(always|from now on|going forward|every time|each time|whenever|any ?time|automatically|just handle|"
                      r"handle (it|this|these|those|them|that) (yourself|for me|going forward|on your own)|you own (it|this|that|these)|"
                      r"(do not|don'?t|no) need to ask|(do not|don'?t) ask( me)?( again| first| anymore)?|without asking|stop asking|"
                      r"standing (ok|okay|approval|permission))\b", re.I)


_ID_PART = re.compile(r'^(\d{4}(-\d{2}){0,2}|\d[\d-]*|[0-9a-f]{8,}|[^:]*@[^:]*|[a-z]{1,3}\d{2,5})$', re.I)


def kind_of(key: str) -> str:
    """The KIND of thing an item is, for a standing OK: its key without the parts that only name this one occurrence
    (a date, a message id, an order or flight number, an address). 'pharmacy:rx-ready:1a1cc3334a28050b' -> 'pharmacy:rx-ready',
    'birthday:casey:YYYY-MM-DD' -> 'birthday:casey'. Real keys end in such ids, so a grant on the whole key would never
    match the next refill. Fewer than two parts left means the kind is too wide to hand over wholesale: ''."""
    parts = [x for x in str(key).split('#')[0].split('~')[0].split(':') if x]
    while parts and _ID_PART.fullmatch(parts[-1]):
        parts.pop()
    parts = [x for x in parts if not re.fullmatch(r'\d{4}(-\d{2}){0,2}', x)][:3]
    return ':'.join(parts) if len(parts) >= 2 else ''


def _grants() -> dict:
    try:
        g = json.loads((LIFE / 'grants.json').read_text())
        return g if isinstance(g, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_grants(g: dict):
    tmp = LIFE / 'grants.json.tmp'
    tmp.write_text(json.dumps(g, indent=2, ensure_ascii=False))
    tmp.replace(LIFE / 'grants.json')


def _his_message(words: str, since: float | None = None):
    """The owner's real message (last 48 hours, their own surfaces) that contains these words, or None.
    A standing OK changes what Zeus does without asking, so it is only ever recorded against the owner's real words.
    since: also not older than this (an answer to an offer cannot be older than the offer)."""
    import chats as C
    msgs = C.messages(max(time.time() - 48 * 3600, float(since or 0)), path=os.environ.get('LIFE_CHAT_DB') or None)
    return C.quoted_in(words, msgs)

LIFE = Path(os.environ.get('LIFE_HOME') or settings.LIFE_HOME)
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
WORDS = {'pending': 'noticed, not worked yet', 'executing': 'being worked now', 'ready': 'worked, not told yet',
         'surfaced': 'told the owner, waiting on the owner\'s word', 'needs_attention': 'work failed', 'done': 'done', 'moot': 'no longer needed',
         'declined': 'the owner passed', 'skipped': 'skipped'}


def db():
    con = sqlite3.connect(LIFE / 'ledger.sqlite3', timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _own_checks_aside(rows: list) -> list:
    """A later check hanging off an item ("<key>#fu1:...") is not a second match for it: when both match, the owner means the
    item, so a short word that matches both an item and its own later check still finds the one item."""
    keys = {r['key'] for r in rows}
    return [r for r in rows if not ('#fu' in r['key'] and r['key'].split('#fu', 1)[0] in keys)]


def find(con, text: str):
    rows = con.execute('SELECT key,state,payload FROM opps').fetchall()
    t = text.strip().lower()
    exact = [r for r in rows if r['key'].lower() == t]
    if exact:
        return exact[0], []
    pre = _own_checks_aside([r for r in rows if r['key'].lower().startswith(t)])
    if len(pre) == 1:
        return pre[0], []
    words = [w for w in t.replace(':', ' ').split() if len(w) > 2]
    scored = []
    for r in rows:
        hay = (r['key'] + ' ' + str(json.loads(r['payload']).get('title') or '')).lower()
        hit = sum(w in hay for w in words)
        if words and hit:
            scored.append((hit, r['state'] in OPEN, r))
    kept = {r['key'] for r in _own_checks_aside([s[2] for s in scored])}
    scored = [s for s in scored if s[2]['key'] in kept]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if scored and (len(scored) == 1 or scored[0][:2] > scored[1][:2]):
        return scored[0][2], []
    return None, [s[2] for s in scored[:6]]


UNSURE = 'a note about it may not have reached the owner; do not say the owner was told and do not ask the owner for an answer'


def state_words(state: str, p: dict) -> str:
    """The item's state in plain words. With the switch commit-integrity-on, an item whose note went out without proof
    that it arrived (told.uncertain) is never called "told the owner". Without the switch, and for every other item: WORDS."""
    if state == 'surfaced' and isinstance(p.get('told'), dict) and p['told'].get('uncertain') and __import__('integrity').on(LIFE):
        return UNSURE
    return WORDS.get(state, state)


def line(r) -> str:
    p = json.loads(r['payload'])
    pk = p.get('packet') or {}
    bits = [f"[{r['key']}] {p.get('title')}  ({state_words(r['state'], p)})"]
    if pk.get('headline'):
        bits.append('   result: ' + pk['headline'])
    d = pk.get('decision')
    if d:
        bits.append('   the owner\'s call: ' + d.get('ask', ''))
    return '\n'.join(bits)


def _when(ts) -> str:
    return datetime.fromtimestamp(float(ts or 0)).strftime('%a %b %d at %I:%M%p').replace(' 0', ' ')


def close(con, r, state: str, by: str, note: str, extra: dict | None = None, revision=None, commit=None):
    """revision: only a background step passes one (the version of the job it is closing). With the switch
    commit-on such a close is refused (-> False, nothing written) when the job was corrected since. The owner's own close
    passes none and is never refused. commit: set by commit.resolve; nobody else passes it."""
    if revision is not None and not __import__('commit').may_close(con, r['key'], revision):
        return False
    p = json.loads(r['payload'])
    if state == 'done' and not __import__('onesend').peer_allows_close(con, r['key'], p):
        return False
    p['closed'] = {'state': state, 'at': time.time(), 'by': by, 'why': note} | (extra or {})
    __import__('commit').on_close(con, r['key'], p, state, by, note, commit)   # commit.py (switch commit-on): the item's commitment ends in this same save; an item without one closes as before
    con.execute('UPDATE opps SET state=?, payload=?, updated=? WHERE key=?', (state, json.dumps(p, default=str), time.time(), r['key']))
    # follow-up checks hanging off a closed item stop too
    for c in con.execute("SELECT key,payload FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready')", (r['key'] + '#fu%',)).fetchall():
        cp = json.loads(c['payload'])
        cp['closed'] = {'state': 'moot', 'at': time.time(), 'by': by, 'why': 'parent item closed'}
        con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=?", (json.dumps(cp, default=str), time.time(), c['key']))
    con.commit()


CLOSED = ('done', 'moot', 'declined', 'skipped')


def free_key(con, key: str):
    """The key a new chat item takes -> (key, None), or (None, the key of the item that is already on the list).
    An item that is still on the list (anything not closed) blocks, as before. A CLOSED item with the same words on the
    same day does not (otherwise `life add` would answer "already on the list" for something long done and save
    nothing). The new item is saved beside the closed one as <key>~2, then ~3, and so on."""
    n, k = 1, key
    while True:
        r = con.execute('SELECT state FROM opps WHERE key=?', (k,)).fetchone()
        if r is None:
            return k, None
        if tuple(r)[0] not in CLOSED:
            return None, k
        n += 1
        k = f'{key}~{n}'


def add(a) -> int:
    """Conversations are a source too: hand something the owner said in chat to the pipes as an item of its own."""
    import re
    from datetime import timedelta
    from ownertime import PACIFIC as pacific  # where the owner is right now, so "+2h" and a time with no zone land on the owner's clock
    opts, words, i = {}, [], 0
    while i < len(a):
        if a[i] in ('--due', '--why') and i + 1 < len(a):
            opts[a[i][2:]] = a[i + 1]
            i += 2
        else:
            words.append(a[i])
            i += 1
    what = ' '.join(words).strip()
    if len(what) < 4:
        print('What should I watch or prepare? Example: life add "Casey lands Sat 3pm on UA 1234, watch the flight" --due YYYY-MM-DDT10:50:00-08:00')
        return 2
    now = datetime.now(pacific)
    due, raw = now, str(opts.get('due') or '').strip()
    m = re.fullmatch(r'\+(\d+)\s*([mhd])', raw)
    if m:
        due = now + timedelta(**{{'m': 'minutes', 'h': 'hours', 'd': 'days'}[m.group(2)]: int(m.group(1))})
    elif raw:
        try:
            t = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            due = (t if t.tzinfo else t.replace(tzinfo=pacific)).astimezone(pacific)
        except ValueError:
            print(f'I could not read the time "{raw}". Use an ISO time or +2h / +3d. Nothing was added.')
            return 2
    slug = re.sub(r'[^a-z0-9]+', '-', what.lower()).strip('-')[:48]
    key = f"chat:{slug}:{due.strftime('%Y-%m-%d')}"
    step = {'do': what, 'action_class': 'prepare_or_research', 'autonomy': 'auto'}
    why = opts.get('why') or 'The owner mentioned it in chat.'
    payload = {'title': what[:120], 'why': why, 'evidence': opts.get('why') or what, 'urgency': 'today', 'dedupe_key': key, 'confidence': 1.0,
               'owner_involved': 'yes', '_source': 'conversation', 'steps': [step], 'proposed_action': what,
               'decision': {'autonomy': 'auto', 'surface_at': due.isoformat(), 'channel': 'text', 'steps': [step], 'granted': False,
                            'reason': 'the owner asked in chat'}}
    with closing(db()) as con:
        key, there = free_key(con, key)        # a closed item with the same words and day does not block: the new one is saved as <key>~2
        if there:
            print('That is already on the list: ' + there)
            return 0
        payload['dedupe_key'] = key
        con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)', (key, 'pending', 'auto', due.isoformat(), 'today', 'prepare_or_research',
                                                                   json.dumps(payload), '[]', time.time(), time.time()))
        __import__('commit').on_add(con, LIFE, key)   # commit.py (switch commit-on): the new item gets its commitment in this same save; without the switch nothing
        con.commit()
    print(f"Added. It will be worked {'now' if due <= now + timedelta(minutes=5) else 'on ' + due.strftime('%a %b %d at %I:%M%p').replace(' 0', ' ')} and the owner hears the result. Key: {key}")
    return 0


def offer_of(p: dict) -> str:
    """The rule an item offers (reflect.py / offers.py), or '' when the item is an ordinary one."""
    o = p.get('offer')
    return str(o.get('rule_key') or '') if isinstance(o, dict) else ''


def _offer_answer(key: str) -> str:
    return (f'This one is an offer to handle something the same way every time from now on, not a prepared step. '
            f'If the owner said yes: life rule yes {key} "<the owner\'s exact words>". If the owner said no: life rule no {key} "<the owner\'s words>". '
            'If the owner has not answered, leave it. Nothing was changed.')


def rules_cmd() -> int:
    import reflect as R
    rows = R.Rules(LIFE).all()
    said = {'proposed': 'not answered yet', 'confirmed': 'the owner said yes', 'declined': 'the owner said no', 'unanswered': 'no answer, will not be asked again',
            'noted': 'only a note, never asked', 'retired': 'taken back', 'adopted': 'you just do it: nothing in it needs the owner\'s yes'}
    print('\n'.join(f"- [{r.get('key')}] ({said.get(r.get('state'), r.get('state'))}) {r.get('when')} -> {r.get('action')}"
                    + (f" | the owner's words: \"{r['his_words']}\"" if r.get('his_words') else '') for r in rows if isinstance(r, dict))
          or 'No standing ways of working yet.')
    return 0


def rule_cmd(a: list) -> int:
    """life rule yes|no <item> [the owner's words]  /  life rule retire <rule> "<why>". The owner's answer to an offer, recorded once."""
    import reflect as O   # the offer helpers live in reflect.py
    import reflect as R
    sub = a[1] if len(a) > 1 else ''
    words = ' '.join(a[3:]).strip()
    if sub not in ('yes', 'no', 'retire') or len(a) < 3:
        print('Use: life rule yes <item> "<the owner\'s exact words>"  |  life rule no <item> [the owner\'s words]  |  life rule retire <rule> "<why>"  |  life rules')
        return 2
    store = R.Rules(LIFE)
    if sub == 'retire':
        key = a[2][len('offer:'):] if a[2].startswith('offer:') else a[2]
        rec = next((x for x in store.all() if x.get('key') == key and x.get('state') in R.OPEN_STATES), None)
        if rec is None:
            print(f'There is no standing way of working called "{key}" to take back. See: life rules')
            return 1
        if len(words) < 3:
            print('Say why (the owner\'s words, or what went wrong): life rule retire %s "<why>". Nothing changed.' % key)
            return 2
        store.retire(key, words)
        gone = O.pref_remove(LIFE, rec.get('pref_text') or O.pref_text(rec))
        with closing(db()) as con:          # an offer for it that is still open has nothing left to ask
            for row in con.execute("SELECT key,state,payload FROM opps WHERE json_extract(payload,'$.offer.rule_key')=?", (key,)).fetchall():
                if row['state'] in OPEN:
                    close(con, row, 'declined', 'zeus chat', 'taken back: ' + words)
        print(f'Taken back: "{rec.get("when")}" is no longer handled that way.'
              + (' (Its line could not be taken out of the preferences file, which is unreadable; tell the developer.)' if gone is None else ''))
        return 0
    with closing(db()) as con:
        r, near = find(con, a[2])
        if r is None:
            print('No single item matches "%s".' % a[2] + (' Close matches:\n' + '\n'.join(line(x) for x in near) if near else ''))
            return 1
        p = json.loads(r['payload'])
        key = offer_of(p)
        if not key:
            print('That item is not an offer of a standing way of working, so there is nothing to answer here. '
                  f'For a prepared step the owner approved use: life approve {r["key"]} "<the owner\'s words>". Nothing changed.')
            return 2
        rec = next((x for x in store.all() if x.get('key') == key and x.get('state') in ('proposed', 'unanswered')), None)
        if rec is None:
            now = next((x.get('state') for x in store.all() if x.get('key') == key), 'gone')
            print(f'The owner\'s answer to this one is already settled ({now}). Nothing changed.')
            return 3
        if sub == 'no':
            store.decline(key, words)
            if r['state'] in OPEN:
                close(con, r, 'declined', 'zeus chat', words or 'the owner said no to this offer')
            print('Recorded the owner\'s no. Nothing changes in how this is handled, and the owner will not be asked about it again.')
            return 0
        if len(words) < 3:
            print(f'Quote what the owner said: life rule yes {r["key"]} "<the owner\'s exact words>". Nothing changed.')
            return 2
        try:
            put_at = con.execute('SELECT first_seen FROM opps WHERE key=?', (r['key'],)).fetchone()[0]
            said = _his_message(words, since=put_at)
        except Exception as e:
            print(f'Could not check the owner\'s words against the owner\'s real messages ({type(e).__name__}), so the owner\'s yes was not recorded. Nothing changed.')
            return 1
        if said is None:
            print('Those words are not in any message the owner sent since this was put to the owner (last two days at most), so the owner\'s yes was not recorded. '
                  'Quote the owner exactly, or ask the owner to say it. Nothing changed.')
            return 3
        if (LIFE / 'tapback-yes-on').exists() and __import__('tapback').not_here(words, said):   # a tapback is never the owner's yes to a rule offer; the owner is asked once in words
            print(__import__('tapback').ASK_IN_WORDS)
            return 9
        text = O.pref_text(rec)
        if not O.pref_add(LIFE, text, words, said['id']):
            print('The preferences file could not be read, so the owner\'s yes was not recorded. Nothing changed. Tell the developer.')
            return 1
        store.confirm(key, words, said['id'], said_at=said['at'], pref_text=text)
        if r['state'] in OPEN:
            # Not closed "by zeus chat": the after-the-yes readback (actcheck.py) reads those as a prepared step that was
            # carried out, and would raise a ticket because nothing was sent or changed. Nothing is meant to be.
            close(con, r, 'done', 'zeus chat, the owner\'s answer to an offer', 'the owner said yes: handled this way every time from now on', {'rule': 'confirmed'})
        print('Recorded the owner\'s yes. From now on this is handled the same way every time:\n- ' + text +
              '\nAnything that spends money, goes to someone outside the owner\'s team, changes the owner\'s calendar or deletes something still waits for the owner\'s yes each time.'
              f'\nIf the owner takes it back: life rule retire {key} "<the owner\'s words>"')
        return 0


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    a = __import__('commit').take_op(a, LIFE)   # commit.py (switch commit-on): "--op <id>" names this request; without the switch the words are untouched
    cmd = a[0] if a else 'list'
    __doc__ = __import__('glue').life_help(globals()['__doc__'], LIFE)   # the help printed below; `life zone` is in it only with the switch file zone-on
    if (LIFE / 'timer-truth-on').exists():   # tab-gaps timers: the help names `life timers` only with its switch file
        __doc__ = __doc__.replace('  life stop  ', '  life timers [--json | --check]   every schedule, follow-up and watch you have running, soonest first (reads only)\n  life stop  ', 1)   # tab-gaps timers
    if __import__('research').on(LIFE):   # the help names `life goal` only while its command answers (research-flow-on)
        __doc__ = __doc__.replace('  life stop  ', '  life goal [list | show <goal> | add "<short title>" --said "<the owner\'s exact words>" | help]\n'
                                  '                               a personal goal the owner gave you in the owner\'s own words: its page, steps and history (`life goal help` has\n'
                                  '                               pause, resume, drop, dates and steps). Never make one up from the owner\'s mail or notes.\n  life stop  ', 1)
    if cmd in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    if cmd == 'company':
        if len(a) != 2 or a[1] in ('-h','--help','help'):
            print('life company ITEM_ID shows the exact pending warning and the reply to use in your direct Zeus chat.')
            return 0 if len(a)==1 or a[1] in ('-h','--help','help') else 2
        with closing(db()) as con:
            row = con.execute("SELECT * FROM research_items WHERE id=? AND state IN ('prepared','pending')",(a[1],)).fetchone()
            print(F.scope_preview(__import__('research')._load(row)) if row else 'No pending warning with that ID.')
        return 0 if row else 2
    if cmd in ('reminder', 'flight'):    # research.py (switch research-flow-on): `life reminder <item>` after the owner's yes, `life flight ...` for a trip's flights; None without the switch, and everything below is as it was
        handled = __import__('research').cli(a, LIFE, db)
        if handled is not None:
            return handled
    if cmd in ('watch', 'signin'):    # lifewatch.py; with the verb's switch file absent it answers None and everything below is as it was
        handled = __import__('lifewatch').main(a, LIFE)
        if handled is not None:
            return handled
    if cmd == 'orders' and (LIFE / 'order-watch-on').exists():    # tab-gaps orders: orderwatch.py, read only; without the switch file this is not a command, as before
        return __import__('orderwatch').cli(a[1:], LIFE)    # tab-gaps orders
    if cmd == 'refund' and (LIFE / 'refund-watch-on').exists():    # refundwatch.py, `refund` lists (read only), `refund back ...` writes down that the owner said something is going back; without the switch file this is not a command, as before
        return __import__('refundwatch').cli(a[1:], LIFE, _his_message)
    if cmd == 'stop':
        (LIFE / 'off').write_text(datetime.now().isoformat())
        print('Life pipes are stopped. Nothing is noticed, worked or sent until: life resume')
        return 0
    if cmd == 'resume':
        (LIFE / 'off').unlink(missing_ok=True)
        print('Life pipes are running again.')
        return 0
    if cmd == 'zone' and __import__('glue').zone_on(LIFE):    # switch file zone-on; without it this is not a command, as before this build
        return __import__('lifezone').main(a[1:], LIFE)
    if cmd == 'commit' and __import__('commit').on(LIFE):    # commit.py (switch commit-on): one JSON object in, one out; without the switch this is not a command, as before
        return __import__('commit').cli(a[1:], LIFE, db)
    if cmd == 'timers' and (LIFE / 'timer-truth-on').exists():    # tab-gaps timers: timers.py (switch timer-truth-on): the one list of every schedule, follow-up and watch; reads only. Without the switch this is not a command, as before
        return __import__('timers').cli(a[1:], LIFE)   # tab-gaps timers
    if cmd == 'why' and (LIFE / 'claims-on').exists():    # claims.py (switch claims-on): who said a thing and when, or that there is no source; without the switch this is not a command, as before
        return __import__('claims').cli(a[1:], LIFE)
    if not (LIFE / 'ledger.sqlite3').exists():
        print('No life ledger yet.')
        return 1
    if __import__('commit').repeated(LIFE, db):    # commit.py (switch commit-on): this exact request (--op) was carried out before
        print('Already done: this request was carried out before. Nothing was changed.')
        return 0
    if cmd in ('goal', 'goals') and __import__('research').on(LIFE):    # goals.py, the goals the owner gave (the owner's words checked by _his_message); it answers only while research-flow-on is there, so there is no new switch
        return __import__('goals').cli(a[1:], LIFE, db, _his_message)
    if cmd == 'add':
        return add(a[1:])
    if cmd == 'correct' and __import__('commit').on(LIFE):    # commit.py (switch commit-on): the owner corrected something about an item; without the switch this is not a command, as before
        return __import__('commit').correct_cmd(a[1:], LIFE, db, find, _his_message)
    if cmd == 'move' and __import__('timing').on(LIFE):    # timing.py (switch merge-timing-on): the owner moves an item themselves, the owner's words kept
        return __import__('timing').move_cmd(a[1:], db)
    if cmd == 'loops' and (LIFE / 'loops-on').exists():    # loops.py (switch loops-on); without it this is not a command, as before
        return __import__('loops').cli(a[1:], LIFE, db)
    if cmd == 'conflicts' and (LIFE / 'conflicts-on').exists():    # conflicts.py (switch conflicts-on); without it this is not a command
        return __import__('conflicts').cli(a[1:], LIFE)
    if cmd == 'relevance':    # relevance.py (4-B: relevance-on folded in)
        return __import__('relevance').cli(a[1:], LIFE, db)
    if cmd == 'rules':
        return rules_cmd()
    if cmd == 'rule':
        return rule_cmd(a)
    if cmd == 'grants':
        g = _grants()
        print('\n'.join(f"- {v.get('title') or k} [{k}]: the owner said \"{v.get('his_words') or v.get('note') or ''}\" ({_when(v.get('said_at') or v.get('at'))})"
                        for k, v in g.items() if isinstance(v, dict)) or 'No standing OKs. Everything that needs the owner\'s yes still waits for it.')
        return 0
    with closing(db()) as con:
        if cmd == 'list':
            rows = con.execute('SELECT key,state,payload FROM opps ORDER BY updated DESC').fetchall()
            rows = rows if '--all' in a else [r for r in rows if r['state'] in OPEN]
            print(('Life pipes are STOPPED (life resume to restart).\n' if (LIFE / 'off').exists() else '') +
                  ('\n'.join(line(r) for r in rows) or 'Nothing open.'))
            return 0
        if len(a) < 2:
            print(__doc__)
            return 2
        r, near = find(con, a[1])
        if r is None:
            print('No single item matches "%s".' % a[1] + (' Close matches:\n' + '\n'.join(line(x) for x in near) if near else ''))
            return 1
        _ci = __import__('integrity')   # integrity.py (switch commit-integrity-on): done, drop and mute close only an item named by its key; a match by loose words is shown, not closed. Without the switch nothing here
        if _ci.on(LIFE) and cmd in _ci.CLOSE_VERBS and not _ci.named_exactly(r['key'], a[1]):
            print('Nothing was changed: "%s" is not the key of an item, and closing one needs its key. The nearest item is:\n%s\n'
                  'If that is the one the owner means: life %s %s%s' % (a[1], line(r), cmd, r['key'], ' "<note>"' if len(a) > 2 else ''))
            return 1
        note = ' '.join(a[2:]).strip()
        p = json.loads(r['payload'])
        _trip = __import__('travel').is_travel(p)   # a trip clock ask (travel.py); False for every other item
        _clash = __import__('conflicts').is_conflict(p)   # a "which one stands?" question (conflicts.py); False for every other item
        _sok = isinstance(p.get('standok'), dict)   # an offer of a standing OK (standok.py puts that mark on its own items only); False for every other item
        if cmd == 'show':
            pk = p.get('packet') or {}
            view = {'key': r['key'], 'state': state_words(r['state'], p), 'title': p.get('title'), 'why': p.get('why'),
                    'evidence': p.get('evidence'), 'result': pk.get('headline'), 'details': pk.get('details'),
                    'his_call': pk.get('decision'), 'followups': pk.get('followups'), 'learned': pk.get('learned'),
                    'told': p.get('told'), 'closed': p.get('closed'), 'his_yes': p.get('approved')}
            # The chat rules do not mention `life approve` yet. This is where the chat Zeus fetches the full prepared
            # step before acting, so the tool says it here, without touching the gated chat rules.
            d = pk.get('decision') or {}
            if _trip:
                view.update(__import__('travel').show_extra(p, r['key'], r['state']))
            elif _clash:
                view.update(__import__('conflicts').show_extra(p, r['key'], r['state']))
            elif _sok:
                view.update(__import__('standok').show_extra(p, r['key'], r['state'], LIFE))   # the exact command for the owner's yes and the owner's no
            elif offer_of(p):
                # An offer of a standing way of working: the owner's answer is recorded with `life rule`, never approve or done.
                if r['state'] in ('surfaced', 'ready') and not p.get('closed'):
                    view['when_he_says_yes'] = (f'Run: life rule yes {r["key"]} "<the owner\'s exact words>". It checks the owner\'s words against the owner\'s real messages and '
                                                'records the owner\'s yes for good: from then on you handle this kind of thing this way every time. '
                                                'Do not use approve or done for this one. An "ok" or a thank-you is not a yes.')
                    view['when_he_says_no'] = (f'Run: life rule no {r["key"]} "<the owner\'s words>". Nothing changes and the owner is never asked about it again.')
            elif r['state'] in ('surfaced', 'ready') and (d.get('ask') or d.get('ready')) and not p.get('approved') and not p.get('closed'):
                view['when_he_says_yes'] = (f'First run: life approve {r["key"]} "<the owner\'s words>". It records the owner\'s yes and prints the exact step. '
                                            f'Do exactly that step, nothing more, then: life done {r["key"]} "<what you did>". '
                                            'An "ok" or a thank-you is not a yes to send, pay, book, change or delete.')
                view['if_he_says_always'] = (f'Only if the owner says to handle this kind of thing every time without asking: life grant {r["key"]} '
                                             '"<the owner\'s exact words>". A plain yes is not that.')
            elif p.get('approved') and not p.get('closed'):
                view['when_he_says_yes'] = (f'The owner\'s yes is already recorded ({_when(p["approved"].get("at"))}). Do not start the step again: check where it went, '
                                            f'then: life done {r["key"]} "<what was done>".')
            view = __import__('onesend').show_more(con, r, p, view, LIFE)   # one-send-on: the exact stored reply and where it stands
            print(json.dumps({k: v for k, v in view.items() if v not in (None, '', [])}, indent=1, ensure_ascii=False, default=str))
            return 0
        decision = (p.get('packet') or {}).get('decision') or {}
        if _trip and cmd == 'approve':
            # The owner's yes to a trip clock ask: code reads the owner's real message and sets the clock for that trip, once (travel.py).
            return __import__('travel').approve(con, r, p, note, LIFE, close)
        if _trip and cmd in ('grant', 'done', 'redraft') and r['state'] in OPEN and not p.get('closed'):
            print(f'This one asks the owner whether the owner\'s clock should follow the owner on a trip, not a prepared step. If the owner said yes: life approve {r["key"]} '
                  f'"<the owner\'s exact words>" (code sets the clock). If the owner said no: life drop {r["key"]} "<the owner\'s words>". If the owner has not answered, leave it. '
                  'Nothing was changed.')
            return 2
        if _clash and cmd == 'approve':
            # The owner's answer to "which one stands?": code reads the owner's real message; the owner's yes keeps one side, the owner's no the other (conflicts.py).
            return __import__('conflicts').approve(con, r, p, note, LIFE, close)
        if _clash and cmd in ('grant', 'done', 'redraft') and r['state'] in OPEN and not p.get('closed'):
            print(f'This one asks the owner which of two things the owner asked for should stand, not a prepared step. Whatever the owner answered: '
                  f'life approve {r["key"]} "<the owner\'s exact words>" (code reads the owner\'s yes or no). If the owner wants both kept: life drop {r["key"]} "<the owner\'s words>". '
                  'If the owner has not answered, leave it. Nothing was changed.')
            return 2
        if _sok and cmd in ('approve', 'grant', 'done', 'redraft', 'ungrant'):   # the owner's answer to an offer of a standing OK is read from the owner's real message by code (standok.py) and written through the same grants file
            return __import__('standok').chat(cmd, con, r, p, note, LIFE, close, _grants, _save_grants)
        if cmd in ('approve', 'grant', 'done') and offer_of(p) and r['state'] in OPEN and not p.get('closed'):
            # The owner's yes to an offer is only ever recorded by `life rule yes` (real words, real message). Closing it as an
            # ordinary step would lose the owner's answer.
            print(_offer_answer(r['key']))
            return 2
        if cmd in ('approve', 'redraft'):
            # Switch file one-send-on (onesend.py): code reads the owner's real reply, carries out the stored step once and keeps
            # the record. Without the switch this answers None and everything below is exactly as it was.
            handled = __import__('onesend').chat(cmd, con, r, p, a[2:], LIFE, close)
            if handled is not None:
                if handled == 3 and cmd == 'approve' and __import__('research').reminder_unsettled(p, r['key']):   # a second yes or a retry on a reminder that never got set is pointed at the one safe step, not left at "do NOT do it again"
                    print(__import__('research').reminder_unsettled(p, r['key']))
                return handled
        if cmd == 'approve':
            # The owner's yes, recorded once, bound to the exact prepared step, so the chat side never acts from an
            # excerpt of the step and the same step can never be done twice.
            held = __import__('glue').yes_held(con, LIFE, r, p)   # glue.py (switch fresh-on): the thing is read again before the owner's yes is recorded; None = as before
            if held is not None:
                return held
            if p.get('closed') or r['state'] not in ('surfaced', 'ready'):
                print(f"This one is not waiting on the owner ({WORDS.get(r['state'], r['state'])}). Do NOT do the step again. "
                      'If the owner is asking for something new, treat it as a new request.')
                return 3
            if not (decision.get('ask') or decision.get('ready')):
                print('Nothing on this one was prepared for the owner\'s yes, so there is nothing to approve. If the owner wants something done, treat it as a new request.')
                return 2
            if p.get('approved'):
                print(f"The owner's yes was already recorded {_when(p['approved'].get('at'))}, and the step was started then. Do NOT do it again. "
                      f"Check where it went (the sent folder, the task, the calendar). If it is done: life done {r['key']} \"<what was done>\". "
                      'If it did not go through, tell the owner exactly that.')
                if __import__('research').reminder_unsettled(p, r['key']):   # the same pointer on the path without one-send-on
                    print(__import__('research').reminder_unsettled(p, r['key']))
                return 3
            p['approved'] = {'at': time.time(), 'by': 'zeus chat', 'his_words': note, 'ask': decision.get('ask'), 'step': decision.get('ready')}
            con.execute('UPDATE opps SET payload=?, updated=? WHERE key=?', (json.dumps(p, default=str), time.time(), r['key']))
            con.commit()
            __import__('authority').legacy_yes(con, LIFE, r['key'], note)   # authority.py (switch authority-on): where this yes comes from, written beside it; without the switch nothing
            print('The owner\'s yes is recorded. Do exactly this now, and nothing more:\n\n' + str(decision.get('ready') or decision.get('ask')) +
                  f'\n\nWhen it is done: life done {r["key"]} "<what you did>". If any part cannot be done exactly as written, '
                  'stop and tell the owner what is in the way. Do not improvise and do not widen it.')
            return 0
        if cmd == 'grant' and (LIFE / 'standing-ok-offer-on').exists():   # while an offer about this very kind is open, the owner's answer belongs to that offer (the narrow standing OK the owner was asked about)
            _held = __import__('standok').held_for_offer(con, LIFE, r['key'])
            if _held:
                print(_held)
                return 2
        if cmd == 'grant':
            # Once the owner OKs automating a recurring thing, the agent owns it from then on: this writes grants.json,
            # which the engine reads.
            gated = [s.get('do') for s in ((p.get('decision') or {}).get('steps') or []) if s.get('do') and (s.get('autonomy') in ('offer', 'ask') or s.get('granted'))]
            ask = decision.get('ready') or decision.get('ask')
            if not gated and not ask:
                print('Nothing on this one needs the owner\'s yes, so there is nothing to give a standing OK for.')
                return 2
            if len(note) < 8 or not STANDING.search(note):
                print('That is not a standing OK. A standing OK needs the owner\'s own words saying to handle this kind of thing every time '
                      '("from now on just refill it", "always handle these", "no need to ask"). For a one-time yes use: '
                      f'life approve {r["key"]} "<the owner\'s words>".')
                return 2
            try:
                said = _his_message(note)
            except Exception as e:
                print(f'Could not check the owner\'s words against the owner\'s real messages ({type(e).__name__}), so no standing OK was recorded. Nothing changed.')
                return 1
            if said is None:
                print('Those words are not in any message the owner sent in the last two days, so no standing OK was recorded. '
                      'Quote the owner exactly, or ask the owner to say it.')
                return 3
            fam = kind_of(r['key'])
            if not fam:
                print('This item has no narrow enough kind to hand over wholesale, so no standing OK was recorded. '
                      f'Keep taking the owner\'s yes each time: life approve {r["key"]} "<the owner\'s words>".')
                return 2
            g = _grants()
            g[fam] = {'at': datetime.now().astimezone().isoformat(), 'note': note, 'his_words': note, 'message_id': said['id'],
                      'said_at': said['at'], 'title': str(p.get('title') or '')[:120], 'from_item': r['key']}
            _save_grants(g)
            F.note(LIFE, f"Standing OK for \"{p.get('title')}\" and things of the same kind: \"{note}\"", r['key'])
            print(f'Standing OK recorded for this kind of item ({fam}). From now on your pipes do the step for these without asking and tell the owner after.\n'
                  'It covers only this kind of item. What it would have asked the owner:\n- ' + '\n- '.join(str(x) for x in (gated or [ask]))
                  + f'\nIf the owner takes it back: life ungrant {fam}')
            return 0
        if cmd == 'ungrant':
            g = _grants()
            fam = kind_of(r['key'])
            if fam not in g:
                print('There is no standing OK for this kind of item.')
                return 1
            del g[fam]
            _save_grants(g)
            __import__('recover').revoked(con, LIFE, fam)   # recover.py (switch recover-on): open items of this kind that have not started lose the standing OK now; nothing without the switch
            print(f'Standing OK removed ({fam}). This kind of thing waits for the owner\'s yes again.')
            return 0
        if cmd == 'told':
            # Same rule as the note (notifier._mark_told): told with a decision = waiting on the owner's word; told without = done.
            if r['state'] != 'ready':
                print(f"Nothing to mark: this one is {WORDS.get(r['state'], r['state'])}.")
                return 0
            p['told'] = {'at': time.time(), 'note': 'chat:' + datetime.now().strftime('%Y-%m-%dT%H:%M'), 'by': 'zeus chat'} | ({'said': note} if note else {})
            state = 'surfaced' if decision else 'done'
            con.execute("UPDATE opps SET state=?, payload=?, updated=? WHERE key=? AND state='ready'", (state, json.dumps(p, default=str), time.time(), r['key']))
            con.commit()
            print('Marked as told in chat: ' + ('it now waits on the owner\'s word.' if state == 'surfaced' else 'your next note will not repeat it.'))
            return 0
        if cmd == 'done':
            # A prepared step closed as done with no recorded yes is allowed (the owner may have done it themselves), and marked.
            unrecorded = r['state'] == 'surfaced' and bool(decision.get('ask') or decision.get('ready')) and not p.get('approved')
            if not p.get('closed') and __import__('research').reminder_unsettled(p, r['key']):   # a reminder that was not read back on the owner's list is never closed as done (the reminder step closes it when it is)
                print('Not closed. ' + __import__('research').reminder_unsettled(p, r['key']))
                return 3
            if close(con, r, 'done', 'zeus chat', note or 'done', {'no_recorded_yes': True} if unrecorded else None) is False:
                print('Still open. The named recipients or downstream outcome lack verified delivery.')
                return 3
            print('Closed as done: ' + str(p.get('title')) + (
                '\n(The owner\'s yes was not recorded on this one. Next time, before doing a prepared step: life approve <item> "<the owner\'s words>".)' if unrecorded else ''))
            return 0
        if cmd == 'drop':
            close(con, r, 'declined', 'zeus chat', note or 'the owner passed on this one')
            if note:
                F.note(LIFE, f"About \"{p.get('title')}\": {note}", r['key'])
            print('Dropped (this item only): ' + str(p.get('title')))
            return 0
        if cmd == 'mute':
            close(con, r, 'declined', 'zeus chat', note or 'the owner said stop')
            fam = F.mute(LIFE, r['key'], str(p.get('title')), note)
            n = 0
            for o in con.execute("SELECT key,state,payload FROM opps WHERE state IN ('pending','executing','ready','surfaced','needs_attention')").fetchall():
                if o['key'] != r['key'] and '#fu' not in o['key'] and F.is_muted(LIFE, o['key']):
                    close(con, o, 'declined', 'zeus chat', 'muted kind: ' + (note or 'the owner said stop'))
                    n += 1
            print(f'Closed and muted: this kind of item ({fam}) will not be raised again.' + (f' {n} other open item(s) of that kind closed too.' if n else ''))
            return 0
        if cmd == 'note':
            if not note:
                print('What did the owner say?')
                return 2
            F.note(LIFE, f"About \"{p.get('title')}\": {note}", r['key'])
            print('Noted.' + (' It was waiting on this from the owner, so it is back at work with the owner\'s answer.' if __import__('recover').supplied(con, LIFE, r['key'], note) else ''))   # recover.py (switch recover-on); 'Noted.' alone without the switch
            return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
