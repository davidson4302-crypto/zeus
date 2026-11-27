"""knowspage: the phone page where the owner sees what Zeus knows about them.

The page shows what Zeus has learned (the owner's own words, the date, how sure it is), the rules and standing OKs
it follows, the people it knows, and what is waiting on the owner, and lets the owner fix a wrong line without
knowing the right chat words. Read-mostly, built for one person on a phone, grouped by day ("Lately") and by person.

How the owner gets it: ONLY from a short-lived link Zeus texts when asked ("show me what you know",
"what have you learned about me"). The same token model as the sign-in handoff (handoff.py): 32 random bytes, kept
only in <life>/knows-page.json (mode 0600) and in the link, good for 2 hours, a wrong or ended token gets the same
plain 404 as any other address. Never a standing link. Asked again while a link has 15+ minutes left, the same link
comes back, so a second ask never kills the first text.

It rides on the sign-in handoff's server (127.0.0.1:8851), under /zeus-signin/know/, so no extra public path is needed:
  GET  /zeus-signin/know/<token>        a page with a Show button and NOTHING personal (iMessage link previews fetch it)
  POST /zeus-signin/know/<token>/show   the content, built here from the life folder at that moment
  POST /zeus-signin/know/<token>/act    a button: first press asks "are you sure?", the second press (with the code
                                        the first one handed back, within 2 minutes) does it
Every change goes through the engine's EXISTING ways of letting a thing go, never a new store:
  a learned fact       ladder.let_go_fact (a note in ladder.json `let_go`, the sweep's shape) + people.let_go
  a correction / rule  ladder.let_go_rule (moved to `rules_archive`, tidy_rules' shape); a rule from the owner's mail
                       habits (standing-rules.json) goes through `life rule retire` itself (lifecli.rule_cmd)
  a promise            the ledger item is dropped exactly as `life drop` drops it (lifecli.close, then prefs.note)
  a standing OK        removed as `life ungrant` removes it (lifecli grants file)
"That's wrong" also leaves a note for the noticer (prefs.note) so Zeus does not go by it again; "Forget this" only
lets go. A change is refused if the thing moved since the page loaded (each button carries a fingerprint of the
record). Writes are made under the tick's own lock. Undo is zeus-snapshot (the files from before are kept there).
Never written: MEMORY.md or any memory outside the life folder. Never shown: file names, switch names, ids, keys,
or lines about topics listed in config `privacy.hidden_topics`. Never logged: tokens, the owner's words, anything shown.

Switch file `knows-page-on` in the life folder. Without it the chat never makes a link and the server answers every
/zeus-signin/know/ address with the plain 404.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import html
import importlib
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def _handoff():
    """handoff.py beside this file: the same module object the server already loaded, or loaded by path under a
    private name (the chat plugin loads this file by path and must not put this folder on its import path)."""
    m = sys.modules.get('handoff')
    if m is not None and Path(getattr(m, '__file__', '')).resolve() == HERE / 'handoff.py':
        return m
    m = sys.modules.get('zeus_knows_handoff')
    if m is None:
        spec = importlib.util.spec_from_file_location('zeus_knows_handoff', HERE / 'handoff.py')
        m = importlib.util.module_from_spec(spec)
        sys.modules['zeus_knows_handoff'] = m
        spec.loader.exec_module(m)
    return m


H = _handoff()
SWITCH = 'knows-page-on'
SESSION_FILE = 'knows-page.json'
LOG_FILE = 'knows-page.log.jsonl'
KEY = 'knows'                      # one page session at a time
TTL = H.TTL                        # 2 hours, as the sign-in link
REUSE_LEFT = 15 * 60
CONFIRM_TTL = 120
SHOW_LIMIT = (20, 600)             # Show at most 20 times per 10 minutes per link
ACT_LIMIT = (30, 600)
WEEK = 7 * 86400
LOCK_WAIT = 8.0
QUOTE_MAX = 240
ROUTE = re.compile(r'^/zeus-signin/know/([A-Za-z0-9_-]{43})(?:/(show|act))?/?$')
PACIFIC = H.PACIFIC                # the owner's configured clock (settings.OWNER_TZ)
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
CLOSED_WORDS = {'done': 'done', 'moot': 'no longer needed', 'declined': 'dropped', 'skipped': 'skipped'}
# A line goes on the page only when it reads as plain words: no path, file name, name_with_underscores, brackets,
# tags or engine words (the chat plugin's own NOT_PLAIN and MACHINERY, plus this page's). Else the line is left out.
NOT_PLAIN = re.compile(r"/Users/|~/|\w\.(?:jsonl?|py|md|sqlite3|ya?ml|db)\b|[A-Za-z]_[A-Za-z]|[{}\[\]`]|<[A-Za-z/][^>]*>"
                       r"|zeus-life|life-engine|\bledger\b|\bpayload\b|\bswitch file\b|\bknows-page\b|\btoken\b|\bdedupe\b", re.I)
# Topics the owner never wants on this page (config privacy.hidden_topics, a list of words). Empty: nothing is hidden.
_HIDDEN_RAW = H.settings.get('privacy.hidden_topics', []) or []
_HIDDEN = [str(t).strip() for t in (_HIDDEN_RAW.split(',') if isinstance(_HIDDEN_RAW, str) else _HIDDEN_RAW) if str(t).strip()]
NOT_OURS = re.compile(r'\b(?:' + '|'.join(re.escape(t) for t in _HIDDEN) + r')\b', re.I) if _HIDDEN else re.compile(r'(?!x)x')
LEVEL_WORDS = {'confirmed': 'you told me to keep this', 'learned': "you've told me more than once",
               'mentioned once': 'you mentioned it once'}
ACT_WORDS = {
    'wrong': ('Mark this as wrong? Zeus will stop going by it.', "Got it. Marked wrong, and Zeus won't go by it anymore."),
    'forget': ('Forget this? Zeus will stop using it.', 'Done. Zeus has let this go.'),
    'stop_rule': ("Stop this one? Zeus won't follow it anymore.", "Done. Zeus won't follow this anymore."),
    'stop_ok': ('Take back this OK? Zeus will ask you each time again.', 'Done. Zeus will ask you each time again.'),
}
MOVED = "This changed since you opened the page, so nothing was done. Tap Show again to see it as it is now."


def on(life) -> bool:
    return (Path(life) / SWITCH).exists()


# ---- the link ---------------------------------------------------------------------------------------------------------

def sessions(life) -> 'H.Sessions':
    return H.Sessions(Path(life), SESSION_FILE)


def link_for(token: str) -> str:
    return f'{H.PUBLIC_BASE}/know/{token}'


def open_link(life, now=None) -> dict:
    """The open session with at least 15 minutes left, else a new one (the old token dies). -> the session."""
    life, now = Path(life), float(now if now is not None else time.time())
    s = sessions(life).get(KEY)
    if s and s.get('state') == 'open' and s.get('token') and float(s.get('expires_ts') or 0) > now + REUSE_LEFT:
        return s
    token = secrets.token_urlsafe(32)
    new = {'site': KEY, 'name': 'what Zeus knows', 'token': token, 'id': H.public_id(token), 'state': 'open',
           'opened_at': H.iso(now), 'opened_ts': now, 'expires_at': H.iso(now + TTL), 'expires_ts': now + TTL}

    def put(d):
        prev = d['sessions'].get(KEY)
        d['sessions'][KEY] = new
        return prev
    prev = sessions(life).change(put)
    if prev and prev.get('state') == 'open':
        log(life, 'replace', id=prev.get('id'), by=new['id'])
    log(life, 'open', id=new['id'])
    return new


def current(life, now=None) -> dict | None:
    """The open, unexpired session, or None."""
    now = float(now if now is not None else time.time())
    s = sessions(life).get(KEY)
    if s and s.get('state') == 'open' and s.get('token') and float(s.get('expires_ts') or 0) > now:
        return s
    return None


def find(life, token: str) -> dict | None:
    return sessions(life).find(token)


def expire(life, now=None) -> bool:
    """Run by the server's reaper: an open session past its 2 hours is closed and its token wiped from the file."""
    now = float(now if now is not None else time.time())
    s = sessions(life).get(KEY)
    if not (s and s.get('state') == 'open' and float(s.get('expires_ts') or 0) <= now):
        return False
    before = sessions(life).close(KEY, 'expired', 'two hours passed', token=s.get('token'))
    if before:
        log(life, 'expire', id=before.get('id'))
    return bool(before)


def ready_line(s: dict, now=None) -> str:
    """The one line Zeus sends when the owner asks. Plain words, the link, how long it works."""
    now = float(now if now is not None else time.time())
    left = max(0, float(s['expires_ts']) - now)
    how_long = 'for the next 2 hours' if left > 110 * 60 else f'for about {max(5, int(left // 300) * 5)} more minutes'
    return f"Here's what I know about you, all on one page: {link_for(s['token'])} The link works {how_long}."


_log_lock = threading.Lock()


def log(life, event: str, **fields):
    """One line per page event. Codes and counts only: never a token, never the owner's words, never what was shown."""
    row = {'at': H.iso(), 'event': event, **{k: v for k, v in fields.items() if v is not None}}
    try:
        with _log_lock:
            fd = os.open(Path(life) / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except OSError:
        pass


# ---- the engine, read where it is ---------------------------------------------------------------------------------------

_eng_lock = threading.RLock()


def _eng(name: str):
    """One of the life engine's modules (handoff.ENGINE_DIR), imported the way handoff.engine() imports signedin:
    appended to the path, no bytecode written there."""
    with _eng_lock:
        sys.dont_write_bytecode = True
        if str(H.ENGINE_DIR) not in sys.path:
            sys.path.append(str(H.ENGINE_DIR))
        return importlib.import_module(name)


def _zone(life):
    try:
        return _eng('ownertime').zone(life=Path(life))     # the owner's clock, also when travelling
    except Exception:
        return PACIFIC


def _fp(*parts) -> str:
    """A fingerprint of a stored record: a button is refused when the record changed since the page loaded."""
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:16]


def _ok(text) -> bool:
    t = str(text or '')
    return bool(t.strip()) and not NOT_PLAIN.search(t) and not NOT_OURS.search(t)


def _one(text, cap=None) -> str:
    t = re.sub(r'\s+', ' ', str(text or '').replace('—', ', ').replace('–', '-')).strip()
    return t if cap is None or len(t) <= cap else t[:cap - 1].rsplit(' ', 1)[0] + '…'


_LEAD = {'has': 'have', 'is': 'are', 'was': 'were', 'wants': 'want', 'likes': 'like', 'needs': 'need', 'prefers': 'prefer',
         'uses': 'use', 'shops': 'shop', 'flies': 'fly', 'lives': 'live', 'works': 'work', 'already': 'already'}


def _to_him(text: str) -> str:
    """'Alex has a store membership.' -> 'You have a store membership.' Only for a sentence that opens on the owner's
    first name (settings.OWNER_FIRST) and a verb listed here; any other sentence is shown as written."""
    first = str(H.settings.OWNER_FIRST or '')
    if not first or first == 'the owner':
        return text
    m = re.match(r"^" + re.escape(first) + r" (\w+)\b(.*)$", text)
    if m and m.group(1) in _LEAD:
        return 'You ' + _LEAD[m.group(1)] + m.group(2)
    return text


def _ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _day(ts, tz, now) -> str:
    d = datetime.fromtimestamp(float(ts), tz).date()
    today = datetime.fromtimestamp(float(now), tz).date()
    if d == today:
        return 'Today'
    if d == today - timedelta(days=1):
        return 'Yesterday'
    return f"{d.strftime('%A, %b')} {d.day}"


def _short(ts, tz) -> str:
    d = datetime.fromtimestamp(float(ts), tz)
    return f"{d.strftime('%b')} {d.day}"


# ---- what goes on the page ------------------------------------------------------------------------------------------------

def _ledger_ro(life):
    db = Path(life) / 'ledger.sqlite3'
    if not db.exists():
        return None
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _promise_item(con, row: dict):
    """The ledger item the quiet pass made from this promise (quietpass.add_life_item puts the message id in it)."""
    if con is None:
        return None
    try:
        hits = con.execute("SELECT key,state,payload,updated FROM opps WHERE json_extract(payload,'$.from_chat.message_id')=? "
                           "AND key NOT LIKE '%#fu%'", (row.get('message_id'),)).fetchall()
    except sqlite3.Error:
        return None
    title = str(row.get('text') or '')[:120]
    return next((h for h in hits if str(json.loads(h['payload']).get('title') or '') == title), hits[0] if len(hits) == 1 else None)


def gather(life, now=None) -> dict:
    """Everything the page shows, read fresh, each changeable thing with its id, its fingerprint and its buttons."""
    life, now = Path(life), float(now if now is not None else time.time())
    LD, Q, F = _eng('ladder'), _eng('quietpass'), _eng('prefs')
    tz = _zone(life)
    rows = Q._read_lines(life / 'learned.jsonl')
    facts = {str(f['id']): f for f in LD.facts(life, now, rows=rows)}
    prefs = Q._read_json(life / 'preferences.json', {})
    prefs = prefs if isinstance(prefs, dict) else {}
    live_rules = LD.rules(prefs, now, LD.replaced_rules(rows))
    out = {'learned': [], 'earlier': [], 'rules': [], 'oks': [], 'muted': [], 'how': [], 'people': [], 'waiting': [], 'tz': tz}
    shown_rules = set()
    con = None
    try:
        con = _ledger_ro(life)
    except sqlite3.Error:
        con = None
    with closing(con) if con is not None else _nothing():
        for row in rows:
            rid, kind = str(row.get('id') or ''), row.get('kind')
            said = _ts(row.get('said_at')) or _ts(row.get('at'))
            if not rid or not said or kind not in ('fact', 'correction', 'promise'):
                continue
            text, quote = _one(row.get('text')), _one(row.get('quote'), QUOTE_MAX)
            if not _ok(text) or (quote and not _ok(quote)):
                continue
            item = {'id': 'f:' + rid, 'text': _to_him(text), 'quote': quote, 'ts': said, 'acts': ['wrong', 'forget']}
            if kind == 'fact':
                f = facts.get(rid)
                if f is None or f['let_go']:
                    continue
                item['level'] = 'you mentioned it in passing' if row.get('passing') else LEVEL_WORDS.get(f['standing'], '')
                item['ver'] = _fp(row, f['let_go'])
            elif kind == 'correction':
                rule = next((r for r in live_rules if str(r.get('message_id')) == str(row.get('message_id'))
                             and _one(r.get('text')) == text), None)
                if rule is None:
                    continue                      # its rule was let go or replaced: nothing of it is in use
                shown_rules.add(rule['id'])
                item.update(level='you corrected me', rule=rule['id'], ver=_fp(row, rule))
            else:
                led = _promise_item(con, row)
                item['level'] = 'you asked me to'
                if led is None:
                    item['acts'] = []             # no item of Zeus's carries it, so there is nothing to stop
                elif led['state'] not in OPEN:
                    item.update(status=CLOSED_WORDS.get(led['state'], 'closed'), acts=[])
                item['ver'] = _fp(row, dict(led) if led is not None else None)
            (out['learned'] if now - said <= WEEK else out['earlier']).append(item)

        try:
            reflect = {r.get('pref_text'): r for r in _eng('reflect').Rules(life).all()
                       if isinstance(r, dict) and r.get('pref_text') and r.get('state') in ('confirmed', 'adopted')}
        except Exception:
            reflect = {}
        for r in live_rules:
            if r['id'] in shown_rules:
                continue
            text = _one(r.get('text'))
            mail = text.startswith(LD.ADOPTED)
            if mail:
                text = re.sub(r'^' + re.escape(LD.ADOPTED) + r'[^:]*:\s*', '', text)
            if text and text[-1] not in '.!?"”)…':
                text += '…'                       # a rule line the engine cut to size is shown as cut, not as finished
            quote = _one(r.get('quote'), QUOTE_MAX) if not mail else ''
            if not _ok(text) or (quote and not _ok(quote)):
                continue
            rec = reflect.get(r.get('text'))
            out['rules'].append({'id': 'r:' + r['id'], 'text': text, 'quote': quote, 'ts': _ts(r.get('at')),
                                 'level': "I picked this up from your mail; you never said it" if mail else 'a rule you set',
                                 'acts': ['stop_rule'], 'ver': _fp(r, rec), 'retire_key': (rec or {}).get('key')})

        try:
            grants = _lifecli(life)._grants()
        except Exception:
            grants = {}
        for fam, g in sorted(grants.items()):
            if not isinstance(g, dict):
                continue
            title, words = _one(g.get('title') or ''), _one(g.get('his_words') or g.get('note') or '', QUOTE_MAX)
            if not _ok(title) or (words and not _ok(words)):
                continue
            out['oks'].append({'id': 'g:' + fam, 'text': title, 'quote': words, 'ts': _ts(g.get('said_at') or g.get('at')),
                               'level': 'you said I can just do this', 'acts': ['stop_ok'], 'ver': _fp(fam, g)})

        for m in F.load(life).get('muted') or []:
            t = _one(m.get('title'))
            if _ok(t):
                out['muted'].append(t)

        out['how'] = _how(life)
        out['people'] = _people(life)
        out['waiting'] = _waiting(con)
    for k in ('learned', 'earlier'):
        out[k].sort(key=lambda i: -i['ts'])
    return out


@contextmanager
def _nothing():
    yield


def _how(life) -> list:
    """how-we-work.md (together.py's page) as [(heading, [lines])], headings without their bracketed notes."""
    try:
        text = (Path(life) / 'how-we-work.md').read_text(encoding='utf-8')
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if line.startswith('## '):
            out.append((re.sub(r'\s*\([^)]*\)\s*$', '', line[3:]).strip(), []))
        elif line.startswith('- ') and out:
            t = _one(line[2:])
            if _ok(t):
                out[-1][1].append(t)
    return [(h, ls) for h, ls in out if ls and _ok(h)]


def _people(life) -> list:
    """[(name, [summary lines])]: the entity pages (pages.summary) when they are there, else the people index."""
    out = []
    try:
        G = _eng('pages')
        ents = G._entities(Path(life))
    except Exception:
        G, ents = None, None
    if G is not None and ents:
        for e in sorted(ents.values(), key=lambda e: (e['kind'] != 'person', e['name'].lower())):
            if NOT_OURS.search(e['name']):
                continue
            try:
                lines = [s['text'] for s in G.summary(e['kind'], e['name'], e['person'], e['src'], e['company'])]
            except Exception:
                continue
            lines = [_one(t) for t in lines if _ok(t) and not t.startswith('Nothing dated is known')]
            if lines and _ok(e['name']):
                out.append((e['name'] + (' (a company)' if e['kind'] == 'company' else ''), lines))
        return out
    try:
        index = _eng('people').load(Path(life))
    except Exception:
        return []
    for p in sorted(index.values(), key=lambda p: str(p.get('name')).lower()):
        lines = [_one(f.get('text')) for f in p.get('facts') or [] if _ok(f.get('text'))]
        if lines and _ok(p.get('name')):
            out.append((str(p['name']), lines[-3:]))
    return out


def _waiting(con) -> list:
    """Things Zeus told the owner about and is waiting on their word for (ledger state 'surfaced'), newest first."""
    if con is None:
        return []
    try:
        rows = con.execute("SELECT key,payload FROM opps WHERE state='surfaced' ORDER BY updated DESC LIMIT 40").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        p = json.loads(r['payload'])
        if p.get('closed'):
            continue
        title = _one(p.get('title'))
        ask = _one(((p.get('packet') or {}).get('decision') or {}).get('ask'))
        if not _ok(title):
            continue
        out.append({'text': title, 'ask': ask if _ok(ask) and ask != title else ''})
    return out


# ---- the page's HTML (a fragment the Show button puts in place) ---------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s or ''), quote=True)


def _item_html(i: dict, tz) -> str:
    meta = ' · '.join(x for x in ((_short(i['ts'], tz) if i.get('ts') else ''), i.get('level') or '', i.get('status') or '') if x)
    said = f'<p class="said">“{_e(i["quote"])}”</p>' if i.get('quote') else ''
    who = '<p class="meta">Your words' + (f' · {_e(meta)}' if meta else '') + '</p>' if i.get('quote') else (
        f'<p class="meta">{_e(meta)}</p>' if meta else '')
    labels = {'wrong': "That's wrong", 'forget': 'Forget this', 'stop_rule': 'Stop this', 'stop_ok': 'Take back'}
    acts = ''.join(f'<button type="button" data-act="{a}">{labels[a]}</button>' for a in i.get('acts') or [])
    acts = f'<div class="acts">{acts}</div><p class="res" role="status"></p>' if acts else ''
    return (f'<li class="item" data-id="{_e(i["id"])}" data-ver="{_e(i.get("ver", ""))}">'
            f'<p class="what">{_e(i["text"])}</p>{said}{who}{acts}</li>')


def render(data: dict, now=None) -> str:
    now = float(now if now is not None else time.time())
    tz = data['tz']
    parts = ['<section><h2>Learned this week</h2>']
    if data['learned']:
        day = None
        for i in data['learned']:
            d = _day(i['ts'], tz, now)
            if d != day:
                parts.append(('</ul>' if day else '') + f'<h3>{_e(d)}</h3><ul class="items">')
                day = d
            parts.append(_item_html(i, tz))
        parts.append('</ul>')
    else:
        parts.append('<p class="none">Nothing new this week.</p>')
    if data['earlier']:
        parts.append(f'<details><summary>Earlier ({len(data["earlier"])})</summary><ul class="items">'
                     + ''.join(_item_html(i, tz) for i in data['earlier']) + '</ul></details>')
    parts.append('</section><section><h2>Rules I follow</h2>')
    parts.append('<ul class="items">' + ''.join(_item_html(i, tz) for i in data['rules']) + '</ul>' if data['rules']
                 else '<p class="none">No other rules right now.</p>')
    parts.append('</section><section><h2>Standing OKs</h2>')
    parts.append('<ul class="items">' + ''.join(_item_html(i, tz) for i in data['oks']) + '</ul>' if data['oks']
                 else '<p class="none">None. Anything that needs your yes still waits for it.</p>')
    parts.append('</section>')
    if data['muted']:
        parts.append('<section><h2>Things you told me to stop raising</h2><ul class="plain">'
                     + ''.join(f'<li>{_e(t)}</li>' for t in data['muted']) + '</ul></section>')
    if data['how']:
        parts.append('<section><h2>How we work together</h2>')
        for h, ls in data['how']:
            parts.append(f'<h3>{_e(h)}</h3><ul class="plain">' + ''.join(f'<li>{_e(t)}</li>' for t in ls) + '</ul>')
        parts.append('</section>')
    parts.append('<section><h2>People and companies</h2>')
    parts.append(''.join(f'<details class="who"><summary>{_e(n)}</summary>' + ''.join(f'<p>{_e(t)}</p>' for t in ls) + '</details>'
                         for n, ls in data['people']) if data['people'] else '<p class="none">Nobody yet.</p>')
    parts.append('</section><section><h2>Waiting on you</h2>')
    if data['waiting']:
        parts.append('<ul class="plain">' + ''.join(f'<li><p class="what">{_e(w["text"])}</p>'
                                                    + (f'<p class="meta">{_e(w["ask"])}</p>' if w['ask'] else '') + '</li>'
                                                    for w in data['waiting']) + '</ul><p class="small">Answer these in chat.</p>')
    else:
        parts.append('<p class="none">Nothing is waiting on you.</p>')
    parts.append('</section><p class="small foot">Changed your mind about something here? Just tell Zeus in chat.</p>')
    return ''.join(parts)


# ---- the buttons ------------------------------------------------------------------------------------------------------------

@contextmanager
def tick_lock(life, wait: float = LOCK_WAIT):
    """The tick's own lock (tick.acquire, pages._lock): nothing here writes beside a running tick. Yields False when
    the tick holds it for longer than `wait` seconds. Writes the time into the file the way tick.acquire does, so a
    tick that finds it busy never reads an old time and calls itself stuck."""
    fh = open(Path(life) / 'tick.lock', 'a+')
    got = False
    try:
        end = time.time() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= end:
                    break
                time.sleep(0.2)
        if got:
            fh.seek(0)
            fh.truncate()
            fh.write(str(time.time()))
            fh.flush()
        yield got
    finally:
        fh.close()


def _lifecli(life):
    """lifecli, pointed at this life folder (its LIFE is read from LIFE_HOME when it is first imported; the server
    serves exactly one life folder, so this only ever sets it to the same place)."""
    L = _eng('lifecli')
    L.LIFE = Path(life)
    return L


def _find_item(data: dict, item_id: str):
    for k in ('learned', 'earlier', 'rules', 'oks'):
        for i in data[k]:
            if i['id'] == item_id:
                return i
    return None


def carry_out(life, item: dict, act: str, now=None) -> tuple:
    """Do one button, through the engine's existing paths. -> (ok, words). Caller holds the tick lock."""
    life, now = Path(life), float(now if now is not None else time.time())
    LD, P, F, Q = _eng('ladder'), _eng('people'), _eng('prefs'), _eng('quietpass')
    kind, ref = item['id'].split(':', 1)
    reason = 'wrong' if act == 'wrong' else 'forgotten'
    if kind == 'f':
        row = next((r for r in Q._read_lines(life / 'learned.jsonl') if str(r.get('id')) == ref), None)
        if row is None:
            return False, MOVED
        if row.get('kind') == 'fact':
            if LD.let_go_fact(life, ref, reason, now) is None:
                return False, MOVED
        elif row.get('kind') == 'correction':
            if LD.let_go_rule(life, item.get('rule') or '', reason, now) is None:
                return False, MOVED
        else:
            L = _lifecli(life)
            with closing(L.db()) as con:
                led = _promise_item(con, row)
                if led is not None and led['state'] in L.OPEN:
                    # exactly `life drop <item> <note>`: this item only, closed as declined, the reason kept as a note
                    note = 'the owner said this was wrong' if act == 'wrong' else 'the owner asked to forget it'
                    L.close(con, led, 'declined', 'the owner\'s phone page', note)
                    F.note(life, f"About \"{json.loads(led['payload']).get('title')}\": {note} (from the owner's phone page)", led['key'])
        P.let_go(life, row)
        if act == 'wrong' and row.get('kind') != 'promise':
            F.note(life, f"The owner said this is wrong, so do not go by it: \"{_one(row.get('text'))}\" (from the owner's phone page)")
        return True, ACT_WORDS[act][1]
    if kind == 'r':
        if item.get('retire_key'):
            # a rule from the owner's mail habits or an offer they said yes to: exactly `life rule retire <rule> "<why>"`
            buf = StringIO()
            with redirect_stdout(buf):
                code = _lifecli(life).rule_cmd(['rule', 'retire', item['retire_key'], 'the owner stopped it from the phone page'])
            return (True, ACT_WORDS[act][1]) if code == 0 else (False, MOVED)
        if LD.let_go_rule(life, ref, 'forgotten', now) is None:
            return False, MOVED
        return True, ACT_WORDS[act][1]
    if kind == 'g':
        L = _lifecli(life)
        g = L._grants()
        if ref not in g:
            return False, MOVED
        del g[ref]                                # exactly `life ungrant`
        L._save_grants(g)
        return True, ACT_WORDS[act][1]
    return False, MOVED


def _allow(stamps: list, limit: int, window: float, now: float) -> bool:
    stamps[:] = [t for t in stamps if now - t < window]
    if len(stamps) >= limit:
        return False
    stamps.append(now)
    return True


class Page:
    """The server's side of the page: Show, and the two-tap buttons. One per server (per life folder)."""

    def __init__(self, life):
        self.life = Path(life)
        self.lock = threading.Lock()
        self.pending = {}                          # confirm code -> what the first tap asked for
        self.shows, self.acts = {}, {}

    def show(self, s: dict, now=None) -> tuple:
        now = float(now if now is not None else time.time())
        with self.lock:
            if not _allow(self.shows.setdefault(s['id'], []), *SHOW_LIMIT, now):
                return 429, {'ok': False, 'words': 'Too many taps. Wait a few minutes and try again.'}
        try:
            data = gather(self.life, now)
            body = render(data, now)
        except Exception as e:
            log(self.life, 'show_failed', id=s['id'], error=type(e).__name__)
            return 503, {'ok': False, 'words': "Zeus couldn't put the page together just now. Try again in a minute."}
        log(self.life, 'show', id=s['id'], learned=len(data['learned']), rules=len(data['rules']), oks=len(data['oks']),
            people=len(data['people']), waiting=len(data['waiting']))
        return 200, {'ok': True, 'html': body}

    def press(self, s: dict, body: dict, now=None) -> tuple:
        now = float(now if now is not None else time.time())
        item_id, act, ver = str(body.get('id') or '')[:300], str(body.get('act') or ''), str(body.get('ver') or '')[:40]
        code = str(body.get('confirm') or '')[:40]
        if act not in ACT_WORDS:
            return 400, {'ok': False, 'words': 'That button did nothing.'}
        with self.lock:
            if not _allow(self.acts.setdefault(s['id'], []), *ACT_LIMIT, now):
                return 429, {'ok': False, 'words': 'Too many taps. Wait a few minutes and try again.'}
            for c in [c for c, p in self.pending.items() if p['exp'] < now]:
                self.pending.pop(c, None)
        if not code:
            item = _find_item(gather(self.life, now), item_id)
            if item is None or act not in item['acts'] or not hmac.compare_digest(item['ver'], ver):
                log(self.life, 'refused', id=s['id'], item=item_id, act=act, why='moved')
                return 409, {'ok': False, 'note': 'moved', 'words': MOVED}
            c = secrets.token_urlsafe(12)
            with self.lock:
                self.pending[c] = {'sid': s['id'], 'item': item_id, 'act': act, 'ver': ver, 'exp': now + CONFIRM_TTL}
            log(self.life, 'ask', id=s['id'], item=item_id, act=act)
            return 200, {'ok': True, 'ask': ACT_WORDS[act][0], 'confirm': c}
        with self.lock:
            p = self.pending.pop(code, None)
        if p is None or p['exp'] < now or (p['sid'], p['item'], p['act'], p['ver']) != (s['id'], item_id, act, ver):
            log(self.life, 'refused', id=s['id'], item=item_id, act=act, why='no confirm')
            return 409, {'ok': False, 'note': 'again', 'words': 'That took too long, so nothing was done. Tap the button again.'}
        with tick_lock(self.life, LOCK_WAIT) as got:
            if not got:
                with self.lock:
                    self.pending[code] = p        # nothing was done: the same "yes" can be tapped again
                log(self.life, 'busy', id=s['id'], item=item_id, act=act)
                return 503, {'ok': False, 'note': 'busy', 'words': 'Zeus is in the middle of something. Tap Yes again in a minute.'}
            item = _find_item(gather(self.life, now), item_id)
            if item is None or act not in item['acts'] or not hmac.compare_digest(item['ver'], ver):
                log(self.life, 'refused', id=s['id'], item=item_id, act=act, why='moved')
                return 409, {'ok': False, 'note': 'moved', 'words': MOVED}
            try:
                ok, words = carry_out(self.life, item, act, now)
            except Exception as e:
                log(self.life, 'failed', id=s['id'], item=item_id, act=act, error=type(e).__name__)
                return 500, {'ok': False, 'words': "That didn't go through, so nothing changed. Tell Zeus in chat instead."}
        log(self.life, 'done' if ok else 'refused', id=s['id'], item=item_id, act=act, why=None if ok else 'moved')
        return (200, {'ok': True, 'done': True, 'words': words}) if ok else (409, {'ok': False, 'note': 'moved', 'words': words})


_shell = {}


def shell() -> str:
    p = HERE / 'knows.html'
    m = p.stat().st_mtime
    if _shell.get('m') != m:
        _shell.update(m=m, text=p.read_text(encoding='utf-8'))
    return _shell['text']
