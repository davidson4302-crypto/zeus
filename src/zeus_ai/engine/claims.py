"""claims: everything Zeus keeps for good knows who said it.

Why: a line in memory such as "the owner approved the Acme renewal" looks exactly like something the owner said. The
stores keep the owner's words and the message they came from for most things; this file reads them back as "who said
this, where, when, and how sure is that", so a sentence somebody else wrote never passes for the owner's own.

Every kept thing (a "claim") is read through this file with five more facts about it:
  speaker       owner | zeus | the named agent or person | unknown
  source_ref    where it came from: chat:<message id>, mail:<mailbox>:<id>, crm:<object>:<id>, item:<key>, or ''
  observed_at   when it was said or read
  verified      owners_words   their own words, quoted, with the message they are in
                owners_channel a real message of theirs, but the kept text is not their exact words
                document    read in a source document (an email, a CRM record)
                relayed     somebody else says so (another agent, a person, Zeus itself repeating a claim)
                unknown     nothing on record says
  supersedes    the older claim(s) this one corrects

The stores (all in the life folder; this file adds no table to the ledger):
  preferences.json       notes, rules (and rules_archive), muted
  learned.jsonl          what the quiet look-back kept from their messages
  people/index.json      the same statements folded into a person's page
  people/sources.json, companies/index.json   what their mail and CRM say (already carry a source and a ref)
  grants.json            their standing OKs
  claims.jsonl           NEW, made on first use: claims recorded through record() and the follow-up of a correction

Rules this file keeps:
- An old record without the five fields is read with honest defaults: a record that carries their quoted words AND the
  message they came from is theirs; anything else is "unknown". Nothing is ever upgraded by a guess.
- `verified` is worked out by code, never taken from a caller. A claim that says it is the owner's must come with a
  message of theirs that really holds the words; otherwise it is kept as relayed (or unknown) under the name of
  whoever handed it over.
- A sentence saying "the owner approved this" is a claim like any other. Confidence never creates authority: nothing
  here can approve anything (authority.py decides that, from other evidence).
- A correction supersedes the older claim. The old one stays on file and is no longer given as the answer. It is
  followed through: taken off the people pages it was folded into, and the open work that used it is LISTED (not
  changed: a job is corrected through `life commit correct`).
- `life why "<claim or question>"` answers in plain words with the source and the date, or says it has no source.
  Never a secret, never a file or field name, never reasoning.

Switch file `claims-on` in the life folder. Without it the write hooks hand back what they were given, `life why` is
not a command, and nothing here is called.

audit_memory(profile_dir) is a one-time, read-only report on Zeus's long-term memory file. It edits nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import settings

SWITCH = 'claims-on'
OWN_FILE = 'claims.jsonl'
FIELDS = ('speaker', 'source_ref', 'observed_at', 'verified', 'supersedes')
VERIFIED = ('owners_words', 'owners_channel', 'document', 'relayed', 'unknown')
RANK = {v: i for i, v in enumerate(VERIFIED)}          # lower = better sourced
LIVE_HOME = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))   # the real life folder (never the LIFE_HOME env of a sandbox)
TEAM_FILE = None                # the team comes from settings.TEAM; a sandbox may name a JSON file with LIFE_TEAM_FILE
PACIFIC = ZoneInfo(settings.OWNER_TZ)     # the owner's clock (the name is kept for callers)
QUOTE_WINDOW = 48 * 3600        # a note is checked against their messages of the last two days (the same window as `life grant`)
# The ways a caller names the owner as a speaker.
HIM = tuple(dict.fromkeys(n for n in ('owner', 'the owner', ' '.join(settings.OWNER_NAME.lower().split()), settings.OWNER_FIRST.lower(),
                                      *(str(x).lower() for x in settings.get('owner.nicknames', []) or [])) if n))
_OWNER_POSS = '|'.join(re.escape(n) + "'?s" for n in HIM if n not in ('owner', 'the owner')) or 'the owner'

# What must never be read back: something that looks like a password, a key, a card or an account number.
SECRET = re.compile(r"(?i)\b(?:pass(?:word|code|phrase)?|pwd|pin|secret|token|api[ _-]?key|bearer|otp|cvv|ssn|login|credential)s?\b"
                    r"(?:\s*(?:is|are|was|=|:)\s*\S+|\s+(?=\S*[\d!@#$%^&*])\S+)"
                    r"|\b(?:sk|pk|rk|ghp|gho|xox[abps])[-_][A-Za-z0-9_\-]{8,}\b|\b[A-Za-z0-9_\-]{28,}\b|\b(?:\d[ -]?){9,}\d\b")
PRIVATE = '[kept private]'

# A sentence that says someone approved, permitted or agreed to something.
APPROVAL = re.compile(r"(?i)\b(?:approv(?:ed|es|als?|e|ing)|authori[sz](?:ed|es|ation)|sign(?:ed|s)? off|green[- ]?lit|green light|go[- ]ahead|pre-?approved"
                      r"|said (?:yes|ok|okay|it(?:'s| is) (?:ok|okay|fine))|gave (?:the |their |her |their )?(?:ok|okay|yes|blessing|consent|permission)"
                      r"|(?:has|have|had|with|got) (?:their|her|their|the owner'?s|" + _OWNER_POSS + r") (?:ok|okay|yes|approval|blessing|consent|permission)"
                      r"|agreed to|consented|cleared (?:it|this|that|to)|standing (?:ok|okay|approval|permission))\b")
PERMISSION = re.compile(r"(?i)\b(?:(?:is|are|was|were) (?:allowed|permitted|cleared|free|authori[sz]ed) to|allowed to|permission to|has permission|may (?:always |now )?"
                        r"(?:send|buy|order|book|pay|delete|post|email|text|message|reply|change|cancel|sign|share|spend|charge|move|approve|use|access|read|write)"
                        r"|can (?:always |now |freely |also )?(?:send|buy|order|book|pay|delete|post|spend|charge|approve|sign|email|text|message|reply|change|cancel|share|move"
                        r"|use|access|read|write|handle|act|decide|route)"
                        r"|without (?:asking|approval|their (?:ok|okay|yes)|a (?:yes|check)|checking)|no (?:need to ask|approval (?:needed|required)|extra (?:yes|approval))"
                        r"|(?:do(?:es)? not|don'?t|doesn'?t|never) (?:need|require)s? (?:to ask|their (?:ok|okay|yes|approval)|approval|a yes|permission)"
                        r"|on (?:their|her|their|the owner'?s|" + _OWNER_POSS + r") behalf|authority to|full (?:access|read/?write|control)|never held|no questions asked|auto-?approve[ds]?)\b")
REPORTING = re.compile(r"(?i)\b(?:reports? (?:in)?to|reporting to|answers? to|works? (?:for|under)|(?:is|are) managed by|manages|supervis(?:es|ed|or of)|oversees"
                       r"|takes? (?:orders|direction|instructions|tasks) from|assigned by|escalates? to|hands? (?:off|over) to|(?:is|are) (?:the )?(?:boss|lead|manager|owner) (?:of|for)"
                       r"|(?:is|are) in charge of|owns|owner of|ownership|assigned to|(?:their|her|their|the owner'?s|" + _OWNER_POSS + r") (?:right|left) hand|co-?pa)\b")

# The part of REPORTING that ties one person to another. The team list names lanes, never who answers to whom.
RELATION = re.compile(r"(?i)\b(?:reports? (?:in)?to|reporting to|answers? to|works? (?:for|under)|managed by|manages|supervis\w+|oversees|from|assigned by"
                      r"|escalates? to|hands? (?:off|over) to|boss)\b")

STOP = frozenset("""a an the and or but so if then than that this these those it its is are was were be been being am do does did done have has had
having i me my mine we us our you your they them their she her they them their to of in on at by for with from into onto over under about as not no yes
ok okay just also too very really can could would should will shall may might must what when where who whom which why how said say says told tell
tells there here now any some all each every other more most much many such own same only once again ever never always please thanks thank want
wants wanted need needs needed get gets got like know knows knew think thinks thought make makes made let lets going gonna zeus owner""".split()
                 + [w for n in HIM for w in n.split()])


# ---- small things --------------------------------------------------------------------------------------------------------

def on(life) -> bool:
    try:
        return life is not None and (Path(life) / SWITCH).exists()
    except (OSError, TypeError):
        return False


def _live(life) -> bool:
    try:
        return Path(life).resolve() == LIVE_HOME.resolve()
    except OSError:
        return False


def _ts(value):
    """A stored time (seconds, or an ISO time with or without the colon in its offset) -> seconds, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s) if float(s) > 0 else None
    except ValueError:
        pass
    for form in (s, re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', s)):
        try:
            t = datetime.fromisoformat(form.replace('Z', '+00:00'))
        except ValueError:
            continue
        return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).timestamp()
    return None


def _iso(ts) -> str | None:
    ts = _ts(ts)
    return datetime.fromtimestamp(ts, PACIFIC).isoformat(timespec='seconds') if ts else None


def _zone():
    try:
        from ownertime import PACIFIC as where_he_is
        return where_he_is
    except Exception:
        return PACIFIC


def day(ts, now=None) -> str:
    """'Nov 9', or 'Nov 9, 2025' when it is not this year. '' when there is no date on record."""
    ts = _ts(ts)
    if not ts:
        return ''
    t = datetime.fromtimestamp(ts, _zone())
    this_year = datetime.fromtimestamp(_ts(now) or time.time(), _zone()).year
    return t.strftime('%b ') + str(t.day) + ('' if t.year == this_year else f', {t.year}')


def _flat(s) -> str:
    s = str(s or '').replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'\s+', ' ', s).strip().lower()


def words(text) -> set:
    """The words of a sentence that carry its meaning (lower case, no filler, plural s dropped)."""
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9'&.@-]*", _flat(text)):
        w = w.strip(".'-").replace("'s", '').replace("'", '')
        if len(w) < 2 or w in STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith('s') and not w.endswith('ss') else w)
    return out


def safe(text, limit: int = 300) -> str:
    """Text that may be read back: anything that looks like a secret is taken out, and it is one line."""
    return SECRET.sub(PRIVATE, ' '.join(str(text or '').split()))[:limit]


def asserts(text) -> list:
    """Which of these a sentence asserts: 'approval', 'permission', 'reporting'. [] for an ordinary sentence."""
    t = str(text or '')
    return [name for name, rx in (('approval', APPROVAL), ('permission', PERMISSION), ('reporting', REPORTING)) if rx.search(t)]


def _read_json(path: Path, default):
    try:
        d = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default
    return d if isinstance(d, type(default)) else default


def _read_lines(path: Path) -> list:
    out = []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def _short(*parts) -> str:
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:10]


def _rule_id(rule: dict) -> str:
    """The same short name ladder.py gives a rule, so a `replaces` written there points at the right one."""
    return str(rule.get('id') or 'rule-' + hashlib.sha1(f"{rule.get('text')}|{rule.get('message_id')}".encode()).hexdigest()[:8])


# ---- their real messages (read-only, through chats.py) ----------------------------------------------------------------------

def chat_store(life):
    """The message store to check their words against: the one named for a sandbox, their real one for the live folder,
    None otherwise (a sandbox never falls back to their real messages)."""
    named = os.environ.get('LIFE_CHAT_DB')
    if named:
        return named
    if _live(life):
        import chats as C
        return C.STORE
    return None


def owners_messages(store, since: float = 0.0, until: float | None = None):
    """Their real messages, oldest first, or None when the store could not be read (never an empty list for that)."""
    if not store:
        return None
    try:
        import chats as C
        return [m for m in C.messages(since, until, path=store) if m['who'] == 'owner']
    except Exception:
        return None


def find_quote(quote, msgs, message_id=None):
    """The message of theirs that really holds these words, or None."""
    if not msgs or len(_flat(quote)) < 3:
        return None
    import chats as C
    return C.quoted_in(str(quote), msgs, message_id)


# ---- the five fields ------------------------------------------------------------------------------------------------------

def defaults(store: str, rec: dict, owner: str = '') -> dict:
    """The honest reading of a record that does not carry the five fields. `owner`: for a line on a person's or a
    company's sourced page, whose page it is."""
    rec = rec if isinstance(rec, dict) else {}
    at = next((t for t in (_ts(rec.get(k)) for k in ('said_at', 'observed_at', 'at', 'read_at')) if t), None)
    out = {'speaker': 'unknown', 'source_ref': '', 'observed_at': _iso(at), 'verified': 'unknown', 'supersedes': None}
    replaces = rec.get('replaces')
    if isinstance(replaces, list) and replaces:
        out['supersedes'] = [str(x) for x in replaces][:8]
    if store == 'page':
        ref = rec.get('ref') if isinstance(rec.get('ref'), dict) else {}
        where = str(rec.get('where') or '')
        if rec.get('source') == 'email' and ref.get('message_id'):
            mine = bool(re.match(r"an email (?:the owner|" + re.escape(settings.OWNER_FIRST) + r") sent\b", where))
            out.update(speaker='owner' if mine else (owner or 'unknown'), source_ref=f"mail:{ref.get('mailbox') or ''}:{ref['message_id']}", verified='document')
        elif ref.get('object') and ref.get('id'):
            out.update(speaker='crm', source_ref=f"crm:{ref['object']}:{ref['id']}", verified='document')
        return out
    quote = str(rec.get('quote') or rec.get('owners_words') or '').strip()
    mid = rec.get('message_id')
    if quote and mid is not None and str(mid) != '':
        out.update(speaker='owner', source_ref=f'chat:{mid}', verified='owners_words')        # the writer kept it only with their words found in that message
    elif store == 'rule' and re.match(r"Learned from (?:his|her|their|the owner's) mail", str(rec.get('text') or '')):
        out.update(speaker='zeus')                                                         # a habit Zeus worked out by itself: nobody said it
    return out


def fields(store: str, rec: dict, owner: str = '') -> dict:
    """The five fields of a record: what it carries, else the honest defaults. A stored `verified` that is not one of
    the known words reads as unknown."""
    base = defaults(store, rec, owner)
    for k in FIELDS:
        if isinstance(rec, dict) and k in rec and rec[k] not in (None, ''):
            base[k] = rec[k]
    ref = str(base['source_ref'] or '')
    # 'said:' is a statement the owner made outside Zeus's chat that was passed on word for word (said:<where>:<when>).
    if base['verified'] not in VERIFIED or (base['verified'] in ('owners_words', 'owners_channel') and not ref.startswith(('chat:', 'said:'))) \
            or (base['verified'] == 'document' and not ref.startswith(('mail:', 'crm:', 'doc:'))):
        base['verified'] = 'unknown'                # a label with nothing behind it is not believed
        if base['speaker'] == 'owner':
            base['speaker'] = 'unknown'
    if isinstance(base['supersedes'], str):
        base['supersedes'] = [base['supersedes']]
    return base


def stamp(life, store: str, rec, owner: str = ''):
    """Write hook. With the switch: the record gets the five fields it does not carry yet (in place). Without it, or on
    any trouble: the record exactly as it was. -> the record.
    A note (store 'note') is Zeus's own account of what they said, so it is Zeus's and unknown, unless its words are
    found in a real message of theirs from the last two days: then it is theirs, with that message."""
    try:
        if not on(life) or not isinstance(rec, dict):
            return rec
        f = fields(store, rec, owner)
        if store in ('note', 'mute') and f['verified'] == 'unknown':
            f['speaker'] = 'zeus'
            said = re.sub(r'^About "[^"]{0,200}":\s*', '', str(rec.get('text') or rec.get('note') or ''))
            msgs = owners_messages(chat_store(life), time.time() - QUOTE_WINDOW) if len(_flat(said)) >= 8 else None
            tries = [said] + re.findall(r'"([^"]{8,600})"', said)
            found = next(((q, h) for q, h in ((q, find_quote(q, msgs)) for q in tries) if h), None)
            if found:
                hit = found[1]
                f.update(speaker='owner', source_ref=f"chat:{hit['id']}", observed_at=_iso(hit['at']), verified='owners_words')
                rec.setdefault('quote', found[0][:600])
                rec.setdefault('message_id', hit['id'])
            elif rec.get('key'):
                f['source_ref'] = f"item:{rec['key']}"
        for k in FIELDS:
            rec.setdefault(k, f[k])
    except Exception:
        pass
    return rec


def rule(life, new: dict):
    """Write hook where a standing rule is written (quietpass.add_rule, ladder.rule). With the switch: the five fields,
    and a rule that replaces older ones is followed through. Without it, or on any trouble: untouched. -> the rule."""
    try:
        if on(life) and isinstance(new, dict):
            stamp(life, 'rule', new)
            for old in new.get('supersedes') or []:
                try:
                    follow(life, old, by=new)
                except Exception:
                    continue
    except Exception:
        pass
    return new


def pages(life, *stores):
    """Write hook in pages.py, just before the sourced pages are saved. With the switch: every line of evidence that
    does not carry the five fields yet gets them (in place). Without it, or on any trouble: untouched."""
    try:
        if not on(life):
            return
        for store in stores:
            for slug, entry in (store or {}).items():
                if isinstance(entry, dict):
                    for ev in entry.get('evidence') or []:
                        stamp(life, 'page', ev, owner=str(entry.get('name') or slug))
    except Exception:
        pass


def learned(life, rows, people=None):
    """Write hook in the quiet look-back, just before its new lines go into learned.jsonl. With the switch: each line
    gets the five fields, and a line that corrects an older one is followed through (the old statement comes off the
    people pages it was folded into; open work that used it is listed in claims.jsonl). Without the switch, or on any
    trouble, the lines are untouched. -> the same list."""
    try:
        if not on(life) or not isinstance(rows, list):
            return rows
        for row in rows:
            stamp(life, 'learned', row)
        for row in rows:
            for old in (row.get('supersedes') or []) if isinstance(row, dict) else []:
                try:
                    follow(life, old, by=row, people=people)
                except Exception:
                    continue
    except Exception:
        pass
    return rows


# ---- reading every store as claims ----------------------------------------------------------------------------------------

def _claim(cid, store, text, rec, owner='', quote=None, where='') -> dict:
    f = fields(store, rec, owner)
    return {'id': str(cid), 'store': store, 'raw': str(text or ''), 'message_id': rec.get('message_id'), 'text': ' '.join(str(text or '').split()), 'quote': ' '.join(str(quote if quote is not None else (rec.get('quote') or rec.get('owners_words') or '')).split()),
            'speaker': str(f['speaker']), 'source_ref': str(f['source_ref'] or ''), 'observed_at': f['observed_at'], 'at': _ts(f['observed_at']),
            'verified': f['verified'], 'supersedes': list(f['supersedes'] or []), 'superseded_by': None, 'let_go': None, 'pages': [], 'where': where,
            'asserts': asserts(text)}


def load(life, now=None) -> list:
    """Every kept claim, from every store, each with the five fields, `superseded_by` and `let_go` (why it no longer
    stands, or None). A store that cannot be read contributes nothing and is named in unreadable(life)."""
    life = Path(life)
    now = _ts(now) or time.time()
    out, gone = [], {}
    prefs = _read_json(life / 'preferences.json', {})
    for n in prefs.get('notes') or []:
        if isinstance(n, dict) and n.get('text'):
            out.append(_claim('note:' + _short(n.get('text'), n.get('at')), 'note', n['text'], n))
    for n in prefs.get('notes_archive') or []:           # a note that expired or that they took back: on file, no longer standing
        if isinstance(n, dict) and n.get('text'):
            out.append(_claim('note:' + _short(n.get('text'), n.get('at')), 'note', n['text'], n))
            lg = n.get('let_go') if isinstance(n.get('let_go'), dict) else {}
            gone[out[-1]['id']] = str(lg.get('why') or ('its last day has passed' if lg.get('reason') == 'expired' else lg.get('reason') or 'it was taken back'))
    for r in prefs.get('rules') or []:
        if isinstance(r, dict) and r.get('text'):
            out.append(_claim(_rule_id(r), 'rule', r['text'], r))
            if str(r.get('expires') or '')[:10] and str(r['expires'])[:10] < datetime.fromtimestamp(now, PACIFIC).date().isoformat():
                gone[_rule_id(r)] = 'its last day has passed'
    for r in prefs.get('rules_archive') or []:
        if isinstance(r, dict) and r.get('text'):
            out.append(_claim(_rule_id(r), 'rule', r['text'], r))
            lg = r.get('let_go') if isinstance(r.get('let_go'), dict) else {}
            gone[_rule_id(r)] = str(lg.get('why') or lg.get('reason') or 'it was taken back')
            if lg.get('by'):
                out[-1]['superseded_by'] = str(lg['by'])
    for m in prefs.get('muted') or []:
        if isinstance(m, dict) and m.get('title'):
            out.append(_claim('mute:' + _short(m.get('family'), m.get('at')), 'mute', f"Stop raising: {m['title']}" + (f" ({m['note']})" if m.get('note') else ''), m, quote=''))
    notes = _read_json(life / 'ladder.json', {})
    let_go = notes.get('let_go') if isinstance(notes.get('let_go'), dict) else {}
    today = datetime.fromtimestamp(now, PACIFIC).date().isoformat()
    by_message = {}
    for row in _read_lines(life / 'learned.jsonl'):
        if not row.get('text'):
            continue
        cid = str(row.get('id') or 'learned:' + _short(row.get('text'), row.get('message_id')))
        c = _claim(cid, 'learned', row['text'], row)
        c['kind'] = row.get('kind')
        out.append(c)
        by_message[(str(row.get('message_id')), str(row['text']).strip())] = c
        note = let_go.get(cid) if isinstance(let_go.get(cid), dict) else None
        if note:
            gone[cid] = str(note.get('why') or note.get('reason') or 'it was let go')
            if note.get('by'):
                c['superseded_by'] = str(note['by'])
        elif str(row.get('expires') or '')[:10] and str(row['expires'])[:10] < today:
            gone[cid] = 'its last day has passed'
    people = _read_json(life / 'people' / 'index.json', {})
    for slug, person in people.items():
        if not isinstance(person, dict):
            continue
        name = str(person.get('name') or slug)
        for f in (person.get('facts') or []) + (person.get('open_threads') or []):
            if not (isinstance(f, dict) and f.get('text')):
                continue
            same = by_message.get((str(f.get('message_id')), str(f['text']).strip()))
            if same is not None:
                same['pages'].append(name)              # the same statement, folded into this person's page
            else:
                c = _claim(f'person:{slug}:' + _short(f.get('text'), f.get('message_id')), 'person', f['text'], f)
                c['pages'] = [name]
                out.append(c)
    for kind, path in (('person', life / 'people' / 'sources.json'), ('company', life / 'companies' / 'index.json')):
        for slug, entry in _read_json(path, {}).items():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name') or (people.get(slug) or {}).get('name') or slug)
            for ev in entry.get('evidence') or []:
                if isinstance(ev, dict) and ev.get('text'):
                    c = _claim(f"page:{kind}:{slug}:{ev.get('id') or _short(ev.get('text'))}", 'page', ev['text'], ev, owner=name, quote='', where=str(ev.get('where') or ''))
                    c['pages'] = [name]
                    out.append(c)
    for fam, g in _read_json(life / 'grants.json', {}).items():
        if isinstance(g, dict):
            said = str(g.get('owners_words') or g.get('note') or '')
            out.append(_claim(f'grant:{fam}', 'grant', f"Standing OK to handle this kind of thing without asking: {g.get('title') or fam}", g, quote=said if g.get('message_id') is not None else ''))
    for row in _read_lines(life / OWN_FILE):
        if row.get('entry') == 'claim' and row.get('text'):
            out.append(_claim(row.get('id') or 'claim:' + _short(row.get('text'), row.get('at')), 'claim', row['text'], row))
        elif row.get('entry') == 'let_go' and row.get('id'):
            gone[str(row['id'])] = str(row.get('why') or 'it was corrected')
    by_id = {c['id']: c for c in out}
    for c in out:
        for old in c['supersedes']:
            if old in by_id and by_id[old] is not c and not by_id[old]['superseded_by']:
                by_id[old]['superseded_by'] = c['id']
    for c in out:
        if c['superseded_by']:
            c['let_go'] = 'it was corrected'
        elif c['id'] in gone:
            c['let_go'] = gone[c['id']]
    return out


def unreadable(life) -> list:
    """The stores that are there and could not be read (so "nothing on file" is never said for them)."""
    life, bad = Path(life), []
    for name, plain in (('preferences.json', 'their notes and rules'), ('people/index.json', 'the people pages'), ('people/sources.json', 'what their mail says about people'),
                        ('companies/index.json', 'the company pages'), ('grants.json', 'their standing OKs')):
        p = life / name
        if p.exists():
            try:
                if not isinstance(json.loads(p.read_text(encoding='utf-8')), dict):
                    bad.append(plain)
            except (OSError, ValueError):
                bad.append(plain)
    return bad


def current(life, now=None) -> list:
    """The claims that still stand."""
    return [c for c in load(life, now) if not c['let_go']]


# ---- recording a claim, and a correction ----------------------------------------------------------------------------------

def _append(life, row: dict) -> None:
    with open(Path(life) / OWN_FILE, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')


def _build(life, text: str, speaker: str, source_ref: str, quote: str, message_id, observed_at, msgs, now) -> dict:
    who = ' '.join(str(speaker or '').lower().split()) or 'unknown'
    row = {'entry': 'claim', 'text': ' '.join(str(text or '').split())[:600], 'quote': ' '.join(str(quote or '').split())[:600], 'said_to_be': who}
    if not row['text']:
        raise ValueError('there is nothing to keep')
    kept = {'speaker': who, 'source_ref': str(source_ref or '')[:200], 'observed_at': _iso(observed_at) or _iso(now), 'verified': 'relayed' if who != 'unknown' else 'unknown'}
    if who in HIM:
        if msgs is None:
            msgs = owners_messages(chat_store(life), 0.0)
        hit = find_quote(row['quote'] or row['text'], msgs, message_id)
        real = hit or next((m for m in msgs or [] if message_id is not None and str(m['id']) == str(message_id)), None)
        if hit:
            kept.update(speaker='owner', source_ref=f"chat:{hit['id']}", observed_at=_iso(hit['at']), verified='owners_words')
            row['quote'] = row['quote'] or row['text']
            row['message_id'] = hit['id']
        elif real:
            kept.update(speaker='owner', source_ref=f"chat:{real['id']}", observed_at=_iso(real['at']), verified='owners_channel')
            row['message_id'] = real['id']
        else:
            kept.update(speaker='unknown', verified='relayed')      # said to be theirs, and nothing of theirs says it: somebody is repeating them
    row.update(kept)
    row['supersedes'] = None
    row['asserts'] = asserts(row['text'])
    row['authorizes'] = False                           # written down so no reader can take a kept sentence for a yes
    row['id'] = 'claim:' + _short(row['text'], row['speaker'], row['source_ref'])       # the same sentence from the same place is one claim
    row['at'] = _iso(now)
    return row


def record(life, text: str, *, speaker: str, source_ref: str = '', quote: str = '', message_id=None, observed_at=None, msgs=None, now=None) -> dict:
    """Keep one claim in claims.jsonl. -> the claim as kept.
    `verified` is decided here, never by the caller. A claim handed over as the owner's is theirs only when `quote` (or the
    text) is really in a message of theirs (`msgs`: their messages; when not given they are read from their store): then
    owners_words. A real message of theirs that does not hold those exact words: owners_channel. No such message: it is kept as
    relayed under "unknown", because somebody is repeating them. Anybody else's claim is relayed under their own name,
    whatever it says about them. A claim with no speaker is unknown. A kept claim never approves anything."""
    life = Path(life)
    row = _build(life, text, speaker, source_ref, quote, message_id, observed_at, msgs, _ts(now) or time.time())
    if not any(r.get('entry') == 'claim' and r.get('id') == row['id'] for r in _read_lines(life / OWN_FILE)):
        _append(life, row)
    return row


def follow(life, old_id, by=None, people=None, con=None) -> dict:
    """A claim was corrected: follow the old one through what used it. -> {'old', 'by', 'pages': names of people pages
    it was taken off, 'work': open items that used it (listed, not changed), 'not_done': what could not be followed}.
    The result is also kept as one line in claims.jsonl. Safe to run again."""
    life = Path(life)
    claims = load(life)
    old = next((c for c in claims if c['id'] == str(old_id)), None)
    out = {'entry': 'followed', 'old': str(old_id), 'by': (by or {}).get('id'), 'pages': [], 'work': [], 'not_done': [], 'at': _iso(time.time())}
    if old is None:
        out['not_done'].append('the older claim is not on file')
        return out
    if old['store'] in ('learned', 'person') and old['pages']:
        try:
            P = people or __import__('people')
            out['pages'] = list(P.let_go(life, {'message_id': old['message_id'], 'text': old['raw']}))
        except Exception as e:
            out['not_done'].append(f'the people pages could not be changed ({type(e).__name__})')
    elif old['pages']:
        out['not_done'].append('it is on a page built from their mail or CRM, which is only changed at its source')
    work = uses(life, old, con=con)
    if work is None:
        out['not_done'].append('the open work could not be read, so what used it is not known')
    else:
        out['work'] = work
    try:                                           # the other stores (notes, the nightly page, what matters, native memory)
        out.update(_elsewhere(life, old, by))
    except Exception as e:
        out['not_done'].append(f'the other stores could not be followed ({type(e).__name__})')
    if not any(r.get('entry') == 'followed' and r.get('old') == out['old'] and r.get('by') == out['by'] for r in _read_lines(life / OWN_FILE)):
        _append(life, out)
    return out


def _same_statement(rec: dict, old: dict) -> bool:
    """Is this record the SAME statement as the claim that was let go? Only by identity: it came from the
    same message of theirs, and its kept words are their same words (one holds the other). Never by alike wording."""
    if old.get('message_id') is None or str(rec.get('message_id')) != str(old['message_id']):
        return False
    a, b = _flat(rec.get('quote') or rec.get('text') or ''), _flat(old.get('quote') or old.get('raw') or '')
    return bool(a and b) and (a in b or b in a)


def _elsewhere(life: Path, old: dict, by=None) -> dict:
    """A retraction that sticks. follow() takes a corrected statement off the people pages and lists the open
    work; this retires the same statement (_same_statement) in each of them
    through that store's own existing way of setting a line aside, and writes down their words and the date with the
    tombstone (the `followed` line in claims.jsonl):
      preferences.json notes   -> moved to `notes_archive` with the reason (the list ladder.tidy_rules uses for dated notes)
      how-we-work.md           -> together.retire (the page is rebuilt at once)
      what-matters.json        -> the reason quoting that message is taken off the entry (the nightly review leaves it out too)
      MEMORY.md, USER.md       -> NOT edited here (native Hermes memory). The exact entry and the `memory` tool call
                                  that removes it are written into the tombstone (`native`), for Zeus's own memory tool.
    Pattern: orchestrator in code over four existing stores. Rejected: a second tombstone database every reader would
    have to learn; each store already has one way to set a line aside."""
    by = by if isinstance(by, dict) else {}
    out = {'said': str(by.get('quote') or by.get('text') or '')[:300], 'said_at': by.get('said_at') or by.get('at') or by.get('observed_at'),
           'notes': 0, 'page_lines': 0, 'matters': 0, 'native': []}
    stamp_now = _iso(time.time())
    p = life / 'preferences.json'
    prefs = _read_json(p, {})
    hit = [n for n in prefs.get('notes') or [] if isinstance(n, dict) and (_same_statement(n, old) or (old['store'] == 'note' and 'note:' + _short(n.get('text'), n.get('at')) == old['id']))]
    if hit:
        prefs['notes'] = [n for n in prefs['notes'] if n not in hit]
        prefs['notes_archive'] = ([n for n in prefs.get('notes_archive') or [] if isinstance(n, dict)]
                                  + [{**n, 'let_go': {'reason': 'corrected', 'why': 'they corrected it', 'by': by.get('id'), 'at': stamp_now}} for n in hit])[-200:]
        tmp = p.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(prefs, indent=2, ensure_ascii=False))
        tmp.replace(p)
        out['notes'] = len(hit)
    T = __import__('together')
    for row in T.read_log(life):
        for kind in (sec[0] for sec in T.SECTIONS):          # the lists the page is built from (a row also holds plain counts)
            for it in row.get(kind) if isinstance(row.get(kind), list) else []:
                if isinstance(it, dict) and it.get('text') and _same_statement(it, old):
                    out['page_lines'] += bool(T.retire(life, kind, str(it['text']), it.get('message_id'), 'they corrected it', {'by': by.get('id')}))
    wm_path = life / 'what-matters.json'
    wm = _read_json(wm_path, {})
    if isinstance(wm.get('order'), list) and old.get('message_id') is not None:
        for x in (x for x in wm['order'] if isinstance(x, dict)):
            keep = [w for w in x.get('why') or [] if not (isinstance(w, dict) and w.get('kind') == 'owners_words' and str(w.get('message_id')) == str(old['message_id']))]
            if len(keep) != len(x.get('why') or []):
                x['why'], out['matters'] = keep, out['matters'] + 1
        if out['matters']:
            tmp = wm_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(wm, indent=2, ensure_ascii=False, default=str))
            tmp.replace(wm_path)
    out['native'] = native_fix(life, old)
    return out


def withdrawn_messages(life, now=None) -> set:
    """The ids of their messages whose kept statement no longer stands (corrected, taken back, or past its last
    day). The nightly review does not quote such a message as the reason an item matters."""
    try:
        return {str(c['message_id']) for c in load(life, now) if c['let_go'] and c['store'] in ('learned', 'rule', 'note', 'person') and c['message_id'] is not None}
    except Exception:
        return set()


def native_fix(life, old: dict) -> list:
    """The exact correction for Zeus's native long-term memory (<profile>/memories/MEMORY.md and USER.md),
    which this engine must not edit. Read-only. -> [{'file', 'tool_call'}]: one ready call of Hermes' own `memory`
    tool (action remove, with the entry's exact opening words) per entry that holds the withdrawn words. An entry
    counts only when it contains their quoted words or the kept sentence whole (12 letters or more), never on likeness."""
    folder = Path(life).resolve().parent.parent / 'memories'
    needles = [n for n in (_flat(old.get('quote') or ''), _flat(old.get('raw') or '')) if len(n) >= 12]
    out = []
    for name, target in (('MEMORY.md', 'memory'), ('USER.md', 'user')):
        try:
            text = (folder / name).read_text(encoding='utf-8')
        except OSError:
            continue
        for entry in (e.strip() for e in text.split('\u00a7')):
            if entry and any(n in _flat(entry) for n in needles):
                out.append({'file': name, 'tool_call': {'action': 'remove', 'target': target, 'old_text': entry[:80]}})
    return out


def uses(life, claim: dict, con=None) -> list:
    """The open items whose own words quote this claim or cite its source. Read-only. [] when there is no ledger yet,
    None when it is there and could not be read."""
    life = Path(life)
    if con is None and not (life / 'ledger.sqlite3').exists():
        return []
    import sqlite3
    own = con is None
    try:
        if own:
            con = sqlite3.connect(f"file:{life / 'ledger.sqlite3'}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT key,payload FROM opps WHERE state IN ('pending','executing','ready','surfaced','needs_attention')").fetchall()
    except Exception:
        return None
    finally:
        if own and con is not None:
            con.close()
    ref = claim.get('source_ref') or ''
    mid = ref.split(':', 1)[1] if ref.startswith('chat:') else None
    needle = _flat(claim.get('quote') or claim.get('text'))
    found = []
    for r in rows:
        key, raw = tuple(r)[0], tuple(r)[1]
        try:
            p = json.loads(raw)
        except (TypeError, ValueError):
            continue
        cm = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
        cited = {str(x.get('id')) for x in cm.get('source_refs') or [] if isinstance(x, dict)} | {str(f.get('source_ref')) for f in cm.get('facts') or [] if isinstance(f, dict)}
        said = _flat(' '.join(str(p.get(k) or '') for k in ('title', 'why', 'evidence', 'proposed_action')))
        if (mid and (mid in cited or ref in cited)) or (len(needle) >= 12 and needle in said):
            found.append({'key': key, 'title': str(p.get('title') or '')[:120], 'job': cm.get('commitment_id')})
    return found


def correct(life, old_id, text: str, *, speaker: str = 'owner', source_ref: str = '', quote: str = '', message_id=None, msgs=None, now=None, con=None) -> dict:
    """A correction: keep the new claim, let the old one go, follow it through the pages and the open work that used
    it. A correction replaces what is on file only when it is at least as well sourced: something somebody relays
    never replaces their own words (it is kept as a claim of its own, and the old one stands).
    -> {'new': the claim kept, 'superseded': bool, 'why': when not, 'followed': what follow() found}"""
    life = Path(life)
    stamp_now = _ts(now) or time.time()
    old = next((c for c in load(life, stamp_now) if c['id'] == str(old_id)), None)
    new = _build(life, text, speaker, source_ref, quote, message_id, None, msgs, stamp_now)
    lines = _read_lines(life / OWN_FILE)
    if old is None or RANK[new['verified']] > RANK[old['verified']]:
        if not any(r.get('entry') == 'claim' and r.get('id') == new['id'] for r in lines):
            _append(life, new)
        return {'new': new, 'superseded': False, 'followed': None,
                'why': 'the older claim is not on file' if old is None else 'it is less well sourced than what is on file, so that still stands'}
    new['supersedes'] = [old['id']]
    if not any(r.get('entry') == 'claim' and r.get('id') == new['id'] for r in lines):
        _append(life, new)
    return {'new': new, 'superseded': True, 'followed': follow(life, old['id'], by=new, con=con)}


# ---- forget: "don't save this" reaches every store of this engine -----------------------------------------------------
# The marker is Zeus's memory plugin's (<profile>/unified-memory/forget.jsonl, read through chats.forgotten). New learning
# never sees the forgotten messages (chats.messages leaves them out); this sweep takes out what was kept BEFORE they asked.
# Unlike a correction (follow/_elsewhere, which set a line aside and keep their words on file), a forget removes the words
# from memory. Their chat log itself (the Hermes store) is not touched. Each store keeps its own
# shape; only the forgotten entries leave it. Pattern: one chain over the stores. Rejected: follow() per learned row,
# which keeps the words in ladder.json, claims.jsonl and the set-aside lists.
FORGET_JSON = ('preferences.json', 'what-matters.json', 'ladder.json', 'how-we-work-retired.json', 'people/state.json')
FORGET_LINES = ('learned.jsonl', 'facts-outbox.jsonl', 'claims.jsonl', 'alignment-log.jsonl')
_WORDS = ('text', 'quote', 'raw', 'statement', 'said', 'evidence', 'why', 'title', 'line')


def _forget_hit(gone, d) -> bool:
    if not isinstance(d, dict):
        return False
    mid = d.get('message_id')
    at = _ts(d.get('said_at') or d.get('observed_at') or d.get('at'))
    if mid is not None and gone.hides('', row_id=mid):
        return True
    return any(isinstance(d.get(k), str) and d[k].strip() and gone.hides(d[k], row_id=mid, at=at) for k in _WORDS)


def _forget_walk(gone, value, count) -> object:
    """Drop every list entry (and every dict value under a key) that is a forgotten statement. The container itself
    stays. count is a one-item list, so a caller can see how much left."""
    if isinstance(value, list):
        keep = []
        for v in value:
            if _forget_hit(gone, v):
                count[0] += 1
            else:
                keep.append(_forget_walk(gone, v, count))
        return keep
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, dict) and _forget_hit(gone, v):
                count[0] += 1
            else:
                out[k] = _forget_walk(gone, v, count)
        return out
    return value


def _forget_plan(life: Path, gone) -> dict:
    """What would leave each store. Read-only."""
    plan = {}
    for name in FORGET_LINES:
        try:
            raw = (life / name).read_text(encoding='utf-8').splitlines(keepends=True)
        except OSError:
            continue
        n, kept = [0], []
        for line in raw:
            try:
                row = json.loads(line)
            except ValueError:
                kept.append(line)                    # a line that will not parse is kept exactly as it was
                continue
            if _forget_hit(gone, row):
                n[0] += 1
                continue
            before = n[0]
            walked = _forget_walk(gone, row, n)
            kept.append(line if n[0] == before else json.dumps(walked, ensure_ascii=False, default=str) + '\n')
        if n[0]:
            plan[name] = (n[0], kept)
    for name in FORGET_JSON:
        p = life / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            plan[name] = (-1, None)                 # unreadable: never written over
            continue
        n = [0]
        kept = _forget_walk(gone, data, n)
        if n[0]:
            plan[name] = (n[0], kept)
    return plan


def forget_sweep(life, profile_home=None, wait: float = 0.0, locked: bool = False, dry: bool = False) -> dict:
    """Take every forgotten statement out of this engine's stores. -> {'state': ok|deferred|failed, 'removed': {store: n}}.
    locked: the caller (the tick) already holds tick.lock. Otherwise the lock is waited for up to `wait` seconds;
    when another tick holds it the work is 'deferred' to that tick's own sweep (it runs first in every tick)."""
    import fcntl
    life = Path(life)
    C = __import__('chats')
    gone = C.forgotten(Path(profile_home) if profile_home else life.resolve().parent.parent)
    if not gone:
        return {'state': 'ok', 'removed': {}}
    plan = _forget_plan(life, gone)
    people_dir = life / 'people' / 'index.json'
    P = __import__('people')
    try:
        index = P.load(life) if people_dir.exists() else {}
    except Exception:
        return {'state': 'failed', 'removed': {}, 'why': 'the people index could not be read'}
    page_hits = sum(1 for person in index.values() for f in ('facts', 'open_threads', 'closed_threads')
                    for i in person.get(f) or [] if _forget_hit(gone, i))
    if not plan and not page_hits:
        return {'state': 'ok', 'removed': {}}
    if dry:
        return {'state': 'ok', 'would_remove': {**{k: v[0] for k, v in plan.items()}, **({'people': page_hits} if page_hits else {})}}
    if any(v[0] < 0 for v in plan.values()):
        return {'state': 'failed', 'removed': {}, 'why': 'a store would not parse; left alone'}
    fh = None
    if not locked:
        fh = open(life / 'tick.lock', 'a+')
        until = time.time() + max(0.0, float(wait))
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= until:
                    fh.close()
                    return {'state': 'deferred', 'removed': {}}
                time.sleep(0.1)
    try:
        removed = {}
        plan = _forget_plan(life, gone)                # again, under the lock
        if page_hits:
            index = P.load(life)
            n, emptied = 0, []
            for slug, person in list(index.items()):
                before = n
                for f in ('facts', 'open_threads', 'closed_threads'):
                    items = person.get(f) or []
                    keep = [i for i in items if not _forget_hit(gone, i)]
                    n += len(items) - len(keep)
                    if len(keep) != len(items):
                        person[f] = keep
                if isinstance(person.get('support'), dict):
                    cut = [0]
                    person['support'] = _forget_walk(gone, person['support'], cut)
                    n += cut[0]
                # Someone known ONLY from what they asked to forget leaves with it (no other line, no relation of theirs).
                if n > before and not any(person.get(f) for f in ('facts', 'open_threads', 'closed_threads', 'support', 'relation', 'aliases')):
                    emptied.append(slug)
                    del index[slug]
            if n:
                P._save(life, index, P._load_state(life))   # the index, then every page rebuilt from it
                for slug in emptied:
                    (life / 'people' / f'{slug}.md').unlink(missing_ok=True)
                removed['people'] = n
                if emptied:
                    removed['people_pages_removed'] = len(emptied)
        for name, (n, kept) in plan.items():
            p = life / name
            tmp = p.with_name(p.name + '.forget.tmp')
            if name.endswith('.jsonl'):
                tmp.write_text(''.join(kept), encoding='utf-8')
            else:
                tmp.write_text(json.dumps(kept, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
            os.replace(tmp, p)
            removed[name] = n
        if 'alignment-log.jsonl' in removed or 'how-we-work-retired.json' in removed:
            T = __import__('together')
            now_dt = T._pacific(time.time())
            T._write(life / T.PAGE, T.render(T.standing(T.read_log(life), now_dt.date(), T.retired_marks(life)), now_dt.date()))
            removed['how-we-work.md'] = 'rebuilt'
        return {'state': 'ok', 'removed': removed}
    except Exception as e:
        return {'state': 'failed', 'removed': {}, 'why': type(e).__name__}
    finally:
        if fh is not None:
            fh.close()


# ---- life why -------------------------------------------------------------------------------------------------------------

def match(life, question: str, now=None) -> dict:
    """The claim on file that best matches a sentence or a question. -> {'claim': the one that stands (or None),
    'replaced': the older one it corrected, when the question matched that}. Word overlap only: no model is asked."""
    asked = words(question)
    if not asked:
        return {'claim': None, 'replaced': None}
    claims = load(life, now)
    by_id = {c['id']: c for c in claims}
    best, top = None, (0.0, 0, 0, 0.0)
    for c in claims:
        if SECRET.search(c['text']) and not (asked & words(SECRET.sub(' ', c['text']))):
            continue
        have = words(c['text'] + ' ' + c['quote'] + ' ' + ' '.join(c['pages']))
        hit = len(asked & have)
        if not hit:
            continue
        share = hit / len(asked)
        if share < 0.5 or (hit < 2 and len(asked) > 1):
            continue
        score = (share, hit, -RANK[c['verified']], c['at'] or 0.0)
        if score > top:
            best, top = c, score
    if best is None:
        return {'claim': None, 'replaced': None}
    stands, seen = best, set()
    while stands['let_go'] and stands['superseded_by'] in by_id and stands['id'] not in seen:
        seen.add(stands['id'])
        stands = by_id[stands['superseded_by']]
    if stands['let_go']:
        return {'claim': None, 'replaced': best, 'gone': stands['let_go']}
    return {'claim': stands, 'replaced': best if stands is not best else None}


def _you(text: str) -> str:
    """A code-written source line ("an email the owner sent from their work inbox") as it is said to the owner."""
    t = str(text or '')
    for name in ('the owner', settings.OWNER_FIRST):
        if name:
            t = re.sub(r"\b" + re.escape(name) + r"'s\b", 'your', t)
            t = re.sub(r"\b" + re.escape(name) + r"\b", 'you', t)
    return re.sub(r'\b(?:their|their)\b', 'your', t)


def said(c: dict, now=None) -> str:
    """One claim as a plain sentence with its source and date."""
    when = day(c['at'], now)
    on_day = f' on {when}' if when else ''
    text, quote = safe(c['text']), safe(c['quote'])
    who = c['speaker']
    if c['verified'] == 'owners_words':
        told = 'You said it outside our chat, and it was passed to me word for word' if c['source_ref'].startswith('said:') else 'You told me'
        return f'{told}{on_day}: "{quote or text}"' + (f' (what I kept from it: {text})' if quote and _flat(quote) != _flat(text) else '')
    if c['verified'] == 'owners_channel':
        return f'You told me{on_day}. I have the message, not your exact words for it. What I kept: {text}'
    if c['verified'] == 'document':
        where = _you(c.get('where') or '') or ('a CRM record' if c['source_ref'].startswith('crm:') else 'a document I read')
        return f'I read it in {where}' + (f', dated {when}' if when else '') + f': {text}'
    if c['verified'] == 'relayed':
        name = 'Someone' if who in ('unknown', '') else 'I' if who == 'zeus' else who[:1].upper() + who[1:]
        verb = 'wrote that down' if who == 'zeus' else 'told me that'
        return (f'{name} {verb}{on_day}. I have not heard it from you, so I treat it as unconfirmed, and it is not your approval for anything. '
                f'What was said: {text}')
    return (f'I have this on file{" from " + when if when else ""}, but nothing on record says who said it, so I cannot call it yours. '
            f'What is on file: {text}')


def why(life, question: str, now=None) -> dict:
    """-> {'found': bool, 'answer': plain words for them, 'claim': the claim it rests on or None}"""
    got = match(life, question, now)
    c, old = got['claim'], got['replaced']
    cannot = unreadable(life)
    tail = (' I could not read ' + ' or '.join(cannot) + ' just now, so this may not be everything.') if cannot else ''
    if c is None and old is not None:
        return {'found': False, 'claim': None,
                'answer': f'I had something on that, and it no longer stands ({got.get("gone")}). I have no current source for it.' + tail}
    if c is None:
        return {'found': False, 'claim': None, 'answer': 'I have no source for that. Nothing I have saved says it.' + tail}
    answer = said(c, now)
    if old is not None:
        before = day(old['at'], now)
        answer += f' That replaced what I had before{" from " + before if before else ""}, which no longer stands.'
    return {'found': True, 'claim': c, 'answer': answer + tail}


def cli(a: list, life) -> int:
    """`life why "<a claim or question>"`"""
    question = ' '.join(a).strip()
    if len(question) < 3:
        print('Why what? Example: life why "they want the dentist reminders on Mondays"')
        return 2
    print(why(life, question)['answer'])
    return 0


# ---- the one-time audit of the long-term memory file (read-only) -----------------------------------------------------------

AUDIT_NOTES = {
    'approval': "no message or saved rule of the owner's backs this. It is not their approval: ask them before acting on it.",
    'permission': "no message or saved rule of the owner's grants this. Treat it as not permitted until they say so.",
    'reporting': "nothing the owner said and nothing in their team list says this. Check with them before routing or reporting on it.",
}


def _cut(text, limit) -> str:
    return safe(text, limit) if limit else safe(text, 2000)


def _team(team_file=None) -> list:
    """The owner's configured team (settings.TEAM) -> [{'name', 'names', 'lane'}]. `team_file` (or LIFE_TEAM_FILE)
    names a pretend list for a sandbox: a JSON list of members, or {"members": [...]}. None when that file cannot be read."""
    members = settings.TEAM
    named = team_file or os.environ.get('LIFE_TEAM_FILE')
    if named:
        try:
            got = json.loads(Path(named).read_text(encoding='utf-8'))
            members = (got.get('members') or got.get('agents') or []) if isinstance(got, dict) else got
        except Exception:
            return None
    return [{'name': str(a.get('name') or ''), 'names': {str(a.get('name') or '').lower()} | {str(x).lower() for x in a.get('aliases') or []},
             'lane': str(a.get('lane') or '')} for a in members or [] if isinstance(a, dict) and a.get('name')]


def audit_memory(profile_dir, *, life=None, chat_db=None, team_file=None, redact: int = 60, now=None) -> dict:
    """Read Zeus's long-term memory file (<profile>/memories/MEMORY.md) and list every line that asserts an approval, a
    permission, or who reports to whom. For each: is it backed by the owner's own records? Looked for, in this order:
      1. their own messages (chats.messages on the profile's store, opened read-only): words the line quotes that are
         really in a message of theirs, or a message of theirs that says the same thing in the same kind of words
      2. their saved rules and standing OKs (the life folder: preferences rules, grants), only those that carry their
         quoted words and the message they came from
      3. the team list (for who is on their team and what their lane is)
    A long line holds several statements: each sentence that asserts something is checked on its own, and the line is
    verified only when every one is backed ("partly" when some are).
    -> a report: counts, and per line {line, kind, starts, statements, backed, verified, source or closest, annotation}.
    `annotation` is the exact note proposed for a line that is not fully verified. NOTHING is edited: every file is
    opened for reading only.
    redact: how many characters of any line or message the report may quote (0 = whole lines; for a private run)."""
    profile = Path(profile_dir)
    life = Path(life) if life else profile / 'workspace' / 'life-engine'
    now = _ts(now) or time.time()
    report = {'what': "Read-only audit of Zeus's long-term memory file: lines that assert an approval, a permission or who reports to whom",
              'at': _iso(now), 'edited': 'nothing', 'quoted_up_to_characters': redact or 'whole lines', 'could_not_read': [], 'lines': []}
    try:
        with open(profile / 'memories' / 'MEMORY.md', 'r', encoding='utf-8') as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        report['could_not_read'].append(f'the memory file ({type(e).__name__})')
        report['counts'] = {'lines_read': 0, 'asserting': 0, 'verified': 0, 'unverified': 0}
        return report
    store = chat_db or (profile / 'state.db')
    msgs = owners_messages(str(store), 0.0) if Path(store).exists() else None
    if msgs is None:
        report['could_not_read'].append('their messages')
    prefs = {}
    if (life / 'preferences.json').exists():
        try:
            prefs = json.loads((life / 'preferences.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            prefs = None
        if not isinstance(prefs, dict):
            report['could_not_read'].append('their saved rules')
            prefs = {}
    rules = [{'what': 'a saved rule of theirs', 'text': str(r.get('text') or ''), 'quote': str(r.get('quote') or ''), 'at': _ts(r.get('at')), 'ref': f"chat:{r.get('message_id')}"}
             for r in prefs.get('rules') or [] if isinstance(r, dict) and r.get('quote') and r.get('message_id') is not None]
    rules += [{'what': 'a standing OK of theirs', 'text': str(g.get('title') or fam), 'quote': str(g.get('owners_words') or ''), 'at': _ts(g.get('said_at') or g.get('at')),
               'ref': f"chat:{g.get('message_id')}"} for fam, g in _read_json(life / 'grants.json', {}).items()
              if isinstance(g, dict) and g.get('owners_words') and g.get('message_id') is not None]
    team = _team(team_file)
    if team is None:
        report['could_not_read'].append('the team list')
    his = [(m, words(m['text'])) for m in msgs or []]
    today = datetime.fromtimestamp(now, PACIFIC).date().isoformat()

    def backing(part: str, kinds: list):
        """One statement -> (its source, None) when the owner's own records back it, else (None, the nearest message of theirs)."""
        want = words(part)
        for q in re.findall(r'["“]([^"”]{12,400})["”]', part):                 # 1a. words it quotes, found in a message of theirs
            hit = find_quote(q, msgs)
            if hit:
                return {'kind': 'their own quoted words', 'ref': f"chat:{hit['id']}", 'on': day(hit['at'], now), 'starts': _cut(hit['text'], redact)}, None
        closest = None
        scored = sorted(((len(want & w) / len(want), len(want & w), m) for m, w in his if want & w), key=lambda x: (x[0], x[1], x[2]['at']), reverse=True) if want else []
        if scored:                                                              # 1b. a message of theirs that says the same, in the same kind of words
            share, hits, m = scored[0]
            closest = {'kind': 'a message of theirs', 'ref': f"chat:{m['id']}", 'on': day(m['at'], now), 'shared_words': hits, 'share_of_statement': round(share, 2)}
            good = next(((sh, h, mm) for sh, h, mm in scored if sh >= 0.6 and h >= 4 and set(kinds) & set(asserts(mm['text']))), None)
            if good:
                return {'kind': 'their own message, same wording', 'ref': f"chat:{good[2]['id']}", 'on': day(good[2]['at'], now), 'shared_words': good[1],
                        'share_of_statement': round(good[0], 2), 'starts': _cut(good[2]['text'], redact)}, None
        for r in rules:                                                         # 2. a saved rule or standing OK that carries their words
            have = words(r['text'] + ' ' + r['quote'])
            if want and len(want & have) >= 4 and len(want & have) / len(want) >= 0.6:
                return {'kind': r['what'], 'ref': r['ref'], 'on': day(r['at'], now), 'starts': _cut(r['quote'], redact)}, None
        # 3. the team list backs only "this teammate's job is <their lane>", said shortly; it never says who answers to whom
        if kinds == ['reporting'] and not RELATION.search(part):
            for t in team or []:
                lane = words(t['lane'])
                if lane and lane <= want and len(want) <= len(lane) + 6 and any(re.search(r'(?<![a-z])' + re.escape(nm) + r'(?![a-z])', part.lower()) for nm in t['names'] if nm):
                    return {'kind': 'the team list', 'ref': 'team:' + t['name'], 'on': '', 'starts': _cut(t['lane'], redact)}, None
        return None, closest

    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        kinds = asserts(line)
        if len(line) < 12 or not kinds:
            continue
        kind = kinds[0]
        # A long line holds several statements. Each sentence that asserts something is checked on its own, and the line
        # counts as verified only when every one of them is backed.
        parts = [x for x in re.split(r'(?<=[.;!?])\s+', line) if len(x) >= 12 and asserts(x)] or [line]
        found = [(asserts(x) or kinds, *backing(x, asserts(x) or kinds)) for x in parts]
        backed = [src for _k, src, _c in found if src]
        entry = {'line': n, 'kind': kind, 'also': kinds[1:], 'starts': _cut(line, redact), 'statements': len(parts), 'backed': len(backed),
                 'verified': True if len(backed) == len(parts) else 'partly' if backed else False, 'source': backed[0] if backed else None}
        if len(backed) > 1:
            entry['more_sources'] = backed[1:]
        named = [t['name'] for t in team or [] if any(re.search(r'(?<![a-z])' + re.escape(nm) + r'(?![a-z])', line.lower()) for nm in t['names'] if nm)]
        if named:
            entry['team_members_named'] = sorted(set(named))
        if entry['verified'] is not True:
            loose = [(k, c) for k, src, c in found if not src]
            entry['closest'] = max((c for _k, c in loose if c), key=lambda c: (c['share_of_statement'], c['shared_words']), default=None)
            note = AUDIT_NOTES[loose[0][0][0]]
            if entry['verified'] == 'partly':
                entry['annotation'] = f" [partly verified, checked {today}: {len(backed)} of {len(parts)} statements here are backed by the owner's own records. For the rest: {note}]"
            else:
                entry['annotation'] = f" [unverified, checked {today}: {note}]"
            entry['proposed'] = 'add the annotation to the end of this line; change nothing else'
        report['lines'].append(entry)
    report['counts'] = {'lines_read': len(lines), 'asserting': len(report['lines']), 'verified': sum(1 for e in report['lines'] if e['verified'] is True),
                        'partly': sum(1 for e in report['lines'] if e['verified'] == 'partly'), 'unverified': sum(1 for e in report['lines'] if e['verified'] is False),
                        'by_kind': {k: sum(1 for e in report['lines'] if e['kind'] == k) for k in ('approval', 'permission', 'reporting')},
                        'owners_messages_searched': len(msgs) if msgs is not None else 'could not be read', 'owners_rules_searched': len(rules),
                        'team_members': len(team) if team is not None else 'could not be read'}
    return report


def main(argv=None) -> int:
    """python3 -I claims.py audit <profile dir> [--out report.json] [--whole]     (reads only; prints or saves the report)
       python3 -I claims.py why <life folder> "<a claim or question>" """
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) >= 2 and a[0] == 'audit':
        out = a[a.index('--out') + 1] if '--out' in a and a.index('--out') + 1 < len(a) else None
        report = audit_memory(a[1], redact=0 if '--whole' in a else 60)
        text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
        if out:
            Path(out).write_text(text + '\n', encoding='utf-8')
            print(json.dumps(report['counts'] | {'could_not_read': report['could_not_read']}, default=str))
        else:
            print(text)
        return 0
    if len(a) >= 3 and a[0] == 'why':
        print(why(a[1], ' '.join(a[2:]))['answer'])
        return 0
    print(main.__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
