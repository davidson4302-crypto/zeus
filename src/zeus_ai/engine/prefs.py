"""What the owner has told Zeus about its proactive help. One small plain file: <LIFE_HOME>/preferences.json

 muted : kinds of things the owner said to stop raising ("stop the tax filing reminder"). Checked by policy (never surfaced) and
         shown to the noticer and the worker.
 notes : things the owner said about an item or about how the owner wants this to work ("I've got that", "leaving from the house").
A one-time "no" on one item is NOT a mute: only an explicit stop or never mutes a family.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def path(life_home) -> Path:
    return Path(life_home) / 'preferences.json'


def load(life_home) -> dict:
    try:
        d = json.loads(path(life_home).read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault('muted', [])
    d.setdefault('notes', [])
    return d


def save(life_home, d: dict):
    p = path(life_home)
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(p)


def family(dedupe_key: str) -> str:
    """Same rule as policy.family: dates and years drop off, first three parts stay."""
    parts = [x for x in str(dedupe_key).split('#')[0].split(':') if not re.fullmatch(r'\d{4}(-\d{2}){0,2}', x)]
    return ':'.join(parts[:3])


def mute(life_home, key: str, title: str, note: str = ''):
    d = load(life_home)
    fam = family(key)
    if not any(m.get('family') == fam and not m.get('company') for m in d['muted']):
        d['muted'].append({'family': fam, 'title': title, 'note': note, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        save(life_home, d)
    return fam


def mute_family(life_home, fam: str, title: str, note: str = ''):
    """Mute a whole kind of thing by its family name (for a standing rule of the owner's, e.g. 'tax-reminder')."""
    d = load(life_home)
    if not any(m.get('family') == fam and not m.get('company') for m in d['muted']):
        d['muted'].append({'family': fam, 'title': title, 'note': note, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        save(life_home, d)
    return fam


def note(life_home, text: str, key: str = ''):
    d = load(life_home)
    d['notes'].append({'text': text[:600], 'key': key, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    __import__('claims').stamp(life_home, 'note', d['notes'][-1])   # claims.py (switch claims-on): who said it, from where, when, how sure; without the switch the note is untouched
    d['notes'] = d['notes'][-60:]
    save(life_home, d)


# A dated note leaves the standing list once its date has passed, so the noticer and the worker are never handed
# notes about a flight or a thread that is already over. One code rule, no model: deciding which notes are stale is
# judging facts, not language.
_SAID_ON = re.compile(r'\((?:owner|the owner|they|their words|he|his words)\b[^)]{0,40}\)', re.I)      # "(the owner, <date>)" is when the owner said it, not a date it ends on
_A_DAY = re.compile(r'\b(20\d\d)-(\d\d)-(\d\d)\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(20\d\d))?')
_MONTH = {m: i + 1 for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'))}


def note_last_day(n: dict):
    """The last calendar day a note names (a date written out: "Nov 16", "November 16, 2026", "2026-11-16"), or None
    when it names none. A day with no year is the one nearest after the note was written. Weekday words and "today"
    are not read: only a written date is sure enough to end a note on."""
    from datetime import date
    try:
        wrote = date.fromisoformat(str(n.get('at') or '')[:10])
    except ValueError:
        return None
    days = []
    for m in _A_DAY.finditer(_SAID_ON.sub(' ', str(n.get('text') or ''))):
        try:
            if m.group(1):
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                d = date(int(m.group(6) or wrote.year), _MONTH[m.group(4)], int(m.group(5)))
                if not m.group(6) and (wrote - d).days > 180:
                    d = d.replace(year=d.year + 1)
        except ValueError:
            continue
        days.append(d)
    return max(days) if days else None


def live_notes(d: dict, today=None) -> list:
    """The notes that still stand: not past the last day they name. (ladder.tidy_rules moves the others to
    `notes_archive`, where they stay as history; this keeps every reader right between two sweeps.)"""
    from datetime import date
    today = today or date.today()
    return [n for n in d.get('notes') or [] if isinstance(n, dict) and not (note_last_day(n) and note_last_day(n) < today)]


def muted_families(life_home) -> set:
    return {m.get('family') for m in load(life_home)['muted'] if m.get('family') and not m.get('company')}


def is_muted(life_home, key: str) -> bool:
    fam = family(key)
    return any(fam == f or fam.startswith(f + ':') for f in muted_families(life_home))


def rules_text(life_home, limit: int = 1500) -> str:
    """The owner's standing rules, learned from what the owner said in chat (quietpass.add_rule), newest 12, one plain line each."""
    d = load(life_home)
    try:    # switch file ladder-on: the owner's confirmed rules first, never one that was let go or is past its last day
        picked = __import__('ladder').rules_text(life_home, d, limit)
    except Exception:
        picked = None
    if picked is not None:
        return picked
    rules = d.get('rules') if isinstance(d.get('rules'), list) else []
    # The owner's own rules first, then habits Zeus learned from the owner's mail (reflect.ADOPTED_LABEL), so a learned habit
    # can never push a rule the owner stated out of the size limit.
    good = [r for r in rules[-12:] if isinstance(r, dict) and r.get('text')]
    learned = [r for r in good if str(r.get('text')).startswith('Learned from')]
    stated = [r for r in good if r not in learned]
    # '- His standing rule:' is the marker the noticer looks for (noticer.py, ladder.py): keep it in step with them.
    return '\n'.join(f"- His standing rule: {r.get('text')}" for r in stated + learned)[:limit]


def as_text(life_home, limit: int = 1500) -> str:
    """Plain lines for the noticer and the worker."""
    d = load(life_home)
    lines = [f"- Stop raising: {m.get('title')}" + (f" ({m['note']})" if m.get('note') else '') for m in d['muted'] if not m.get('company')]
    lines += [f"- The owner said: {n.get('text')}" for n in live_notes(d)[-12:]]   # a note past the date it names is history, not a standing line
    return '\n'.join(lines)[:limit]

# Company scope comes from a direct authenticated owner event about an exact
# prepared notice. No mail body, recipient, provider domain or model can write it.
def notice_revision(item):
    import hashlib
    evidence = sorted(item['evidence'], key=lambda e:(str(e.get('ref')),str(e.get('fp'))))
    packet = item['payload']
    revision = {'id':item['id'], 'title':item['title'], 'evidence':evidence,
                'packet':{k:packet.get(k) for k in ('action','prepared_work','needs_his_yes','handoff','with_items')}}
    return hashlib.sha256(json.dumps(revision, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def scope_preview(item):
    topic = item['payload'].get('action') or item['title']
    content = item['payload'].get('prepared_work',{}).get('ready') or ''
    refs = ', '.join(str(e['ref']) for e in item['evidence'])
    return (f"{item['title']}\n{topic}\n{content}\nSources: {refs}\n"
            f"Only if every operation in this notice belongs to one company and topic, reply in your direct Zeus chat:\n"
            f"Company for {notice_revision(item)[:24]}: COMPANY | TOPIC")


def owner_scope(life, words, event_ref, event_time):
    import sqlite3
    from contextlib import closing
    match = re.fullmatch(r'Company for ([0-9a-f]{24}): ([^|\r\n]{1,100}) \| ([^|\r\n]{1,80})', words.strip(), re.I)
    if not match or not event_ref or not event_time:
        return None
    token, company, topic = (s.strip() for s in match.groups())
    if not company or not topic:
        return 'Company or topic is missing.'
    with closing(sqlite3.connect((Path(life)/'ledger.sqlite3').as_uri()+'?mode=rw', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        found = []
        for row in db.execute("SELECT id,title,evidence,payload FROM research_items WHERE state IN ('pending','prepared')"):
            item = dict(row, evidence=json.loads(row['evidence']), payload=json.loads(row['payload']))
            revision = notice_revision(item)
            if revision.startswith(token.lower()):
                found.append((item,revision))
        if len(found) != 1:
            return 'That warning has changed or is no longer pending. Review its current version.'
        item, revision = found[0]
        db.execute("""CREATE TABLE IF NOT EXISTS owner_notice_scope(
            source_event TEXT NOT NULL, at REAL NOT NULL, revision TEXT NOT NULL, company TEXT NOT NULL, topic TEXT NOT NULL,
            PRIMARY KEY (source_event,at,revision,company,topic))""")
        db.execute("CREATE INDEX IF NOT EXISTS owner_notice_revision ON owner_notice_scope(revision, source_event)")
        db.execute("INSERT OR IGNORE INTO owner_notice_scope VALUES(?,?,?,?,?)",
                   (json.dumps(event_ref), event_time, revision, company.casefold(), topic.casefold()))
        valid = current_company(db, item)
        db.commit()
        if valid is None:
            return 'The recorded messages conflict. Please confirm the company and topic in a new direct message.'
        if valid != (company.casefold(),topic.casefold()):
            return 'Kept your newer company and topic for this warning.'
    return 'Saved the subject company and topic for that exact warning.'


def current_company(db, item):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notice_scope'").fetchone():
        return None
    revision = notice_revision(item)
    # Resolve edits by the provider's original message ID before using the most recent
    # owner decision. An edit that moves an assignment to a new notice revokes the old one.
    rows = db.execute("""WITH related AS (
            SELECT DISTINCT source_event FROM owner_notice_scope WHERE revision=?),
        latest AS (SELECT source_event,MAX(at) AS at FROM owner_notice_scope JOIN related USING(source_event)
            GROUP BY source_event)
        SELECT at,revision,company,topic FROM owner_notice_scope JOIN latest USING(source_event,at)
        ORDER BY at DESC""", (revision,)).fetchall()
    choices = {(r[1],r[2],r[3]) for r in rows if r[0]==rows[0][0]}
    if len(choices)==1:
        target,company,topic = choices.pop()
        if target==revision:
            return company,topic
    return None


def company_rule(life, db, item, cited='', preference_skip=False):
    """Verify a typed company and topic for every member of a prepared notice."""
    rules = load(life)['muted']
    scoped = [r for r in rules if r.get('company') and r.get('company_rule_source') and r.get('topic')]
    if not scoped:
        return 'ordinary'
    members = [item, *(item.get('with') or [])]
    companies = [current_company(db,m) for m in members]
    titles = ' '.join(str(e.get('title') or '') for m in members for e in m['evidence'])
    applicable = [r for r in scoped if r['family']==cited or 
                  any(c and c[1]==r['topic'].casefold() for c in companies) or
                  re.search(r'(?<!\w)'+re.escape(r['topic'])+r'(?!\w)',titles,re.I)]
    global_families = {r['family'] for r in rules if r.get('family') and not r.get('company')}
    for rule in applicable:
        if rule['family'] in global_families and (all(c and c[1]==rule['topic'].casefold() for c in companies) or
                re.search(r'(?<!\w)'+re.escape(rule['topic'])+r'(?!\w)',titles,re.I)):
            return 'global'
        if all(c == (rule['company'].casefold(),rule['topic'].casefold()) for c in companies):
            return 'verified'
    keys = [item['opportunity_key'],item['opp_key'],*item['dedup_keys']]
    if any(any(family(key)==f or family(key).startswith(f+':') for key in keys) for f in global_families):
        return 'global'
    if not applicable and not preference_skip:
        return 'ordinary'
    return 'other company' if all(companies) else 'company unverified'
