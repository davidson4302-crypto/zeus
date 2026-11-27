"""pages: one page per person and per company the owner deals with. A short current summary on top, a dated,
sourced timeline below.

Without pages, "What's the story with Alex?" or "where are we with Acme?" has nothing to answer from. Each page is a
summary that is rewritten as evidence changes, then an append-only timeline where every line says where and when it
came from (the <!-- timeline --> separator, dated lines, citations, both claims kept when sources disagree). It is
filled from the owner's own mail and, optionally, a HubSpot CRM account.

Where facts come from, highest trust first:
  1. the owner's own words in chat   people.py's index (learned.jsonl, quote-checked). Never written or changed here.
  2. email                           mail the owner SENT from the configured mailboxes, and the newest mail each person
                                     sent back
  3. HubSpot (optional)              the configured CRM account (contacts, companies, deals), read only; off unless
                                     `pages.hubspot_account` and `pages.hubspot_portal` are set
When two sources say different things about who someone is or where they work, the summary keeps both and says so,
the owner's words first. Nothing silently overwrites anything: timeline lines are only ever added (by id), never edited.

Files in the life folder:
  people/index.json      people.py's, unchanged: built only from the owner's own chat words
  people/sources.json    slug -> what email and HubSpot say about a person (same slug as index.json when joined)
  companies/index.json   slug -> a company: name, domains, HubSpot id, the people there, its sourced lines
  people/<slug>.md, companies/<slug>.md   the readable pages (summary, <!-- timeline -->, then the dated lines)
  pages-seeded.json, pages-seed.log.jsonl the one-time seed's marker and its log (how many reads, how many pages)

Switch file `pages-on` in the life folder: the chat is handed the page of a person or company the owner's message
names (life-items plugin, pages_section), and people.py renders the owner's people pages in this layout. Off: people
pages, the noticer and every chat turn are unchanged. The noticer is never handed email or HubSpot lines either way.

The seed (`seed`) reads the mail and HubSpot ONCE, by hand. Nothing schedules it: no tick, no cron job, no hook calls
it, and a second real run is refused unless --again is given. A real run also needs --checked, given only after a
dry run (`seed --dry`) has shown the counts and 5 sample pages (a pause for a quality check).
Only people who wrote back in the window, or are on a real HubSpot deal, get a page: newsletters, robots, no-reply
senders, the owner's own agents' inboxes, bulk sends and one-way cold outreach do not. Deals matching
`pages.skip_deal_words` and HubSpot's test and sample rows are never read into a page.
Reads go only through allow-listed read actions; there is no write path to mail or HubSpot in this file.

Freshness: newest(entity) and newest_for(life_home, name) give the newest dated source line in code, and each page
carries it as one line: "Newest source: Nov 5, 2026, <where it came from>." freshness() below sits behind its own
switch file `freshness-on` (acts only together with `pages-on`).

contact() below hands the chat, for "has X replied?"-type questions, who wrote last, what is owed or already settled
(loops.settled) and the owner's own recent words about that person, behind its own switch file `contact-facts-on`
(acts only together with `pages-on`); with it, such a question may also find a mail-only person by first name
(_match wide=True).
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import people as P  # noqa: E402
import settings  # noqa: E402

OWNER = settings.OWNER_FIRST     # how page lines name the owner ("Alex emailed Jordan ...")
OWNER_RE = re.escape(OWNER)
IN_CHAT = f"{OWNER}'s own words in chat"

PAGES_ON = 'pages-on'
SENTINEL = '<!-- timeline -->'
NEWEST_LINE = re.compile(r'^Newest source: (?P<date>[A-Z][a-z]{2} \d{1,2}, \d{4}), (?P<label>.+)\.$', re.M)
SUMMARY_MAX = 3                  # "a 3-line current answer": the summary is at most three lines (plus a "sources differ" line)
TIMELINE_SHOWN = 30
EVIDENCE_KEPT = 40               # per page; the oldest email lines beyond this are not kept (HubSpot lines always are)

# Mailbox label (settings.OWNER_EMAILS) -> how a page line names that inbox. Config `mail.mailbox_names` may give a
# friendlier name per label; otherwise the label itself is used.
_BOX_NAMES = dict(settings.get('mail.mailbox_names', {}) or {})
MAILBOX_NAME = {label: str(_BOX_NAMES.get(label) or label) for label in settings.OWNER_EMAILS}


def sent_from(box: str) -> str:
    """A line's 'where' for mail the owner sent: "an email Alex sent from Alex's work inbox"."""
    return f"an email {OWNER} sent from {OWNER}'s {MAILBOX_NAME.get(box, box)} inbox"


def sent_to(first: str, box: str) -> str:
    """A line's 'where' for mail someone sent the owner: "an email from Taylor to Alex's work inbox"."""
    return f"an email from {first} to {OWNER}'s {MAILBOX_NAME.get(box, box)} inbox"
# The owner's own addresses (settings.OWNER_EMAILS + settings.OWN_ADDRESSES). A page is never made for the owner.
OWN = {a.lower() for a in settings.OWNER_EMAILS.values()} | set(settings.OWN_ADDRESSES)
# Agent inboxes (this agent's and any other assistant the owner runs) are bots, not people the owner deals with.
AGENT_DOMAINS = ('agentmail.to', 'inkboxmail.com') + tuple(settings.get('pages.agent_domains', []) or [])
ROBOT_LOCAL = re.compile(r'^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|notify|support|help|mailer[-_.]?daemon|postmaster|'
                         r'bounces?|alerts?|newsletters?|news|marketing|billing|invoices?|receipts?|accounts?[-_.]?payable|'
                         r'calendar|calendar-notification|automated|auto|system|admin|webmaster)(?:[-_.+].*)?$', re.I)
ROBOT_DOMAIN = re.compile(r'(?:^|\.)(?:resource\.calendar\.google\.com|calendar\.google\.com|docs\.google\.com|reply\.github\.com|'
                          r'github\.com|hubspot\.com|hs-inbox\.com|hubspotemail\.net|sendgrid\.net|mailchimp\.com|mcsv\.net|'
                          r'amazonses\.com|bounce\..+|e\..+|em\d*\..+|mail\..+\.(?:com|net)'
                          r'|unsub[^.]*\..+|[^.]*no-?reply[^.]*\..+)$', re.I)   # unsub.<list host>, unsubscribe2.<host>, <name>-noreply.com
ROLE_NAME = re.compile(r'\b(?:team|support|sales|billing|accounts|payable|receivable|office|admin|orders|service|customer|'
                       r'notifications?|no[- ]?reply|info|hr|helpdesk|reception|inbox|desk|robot|bot)\b', re.I)
FREE_MAIL = {'gmail.com', 'googlemail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com', 'me.com', 'mac.com',
             'aol.com', 'live.com', 'msn.com', 'comcast.net', 'att.net', 'sbcglobal.net', 'protonmail.com', 'proton.me',
             'ymail.com', 'verizon.net', 'cox.net', 'charter.net'}
# Deals whose name or pipeline holds one of these words belong to someone else and are never read into a page
# (config `pages.skip_deal_words`, default none).
_SKIP_DEAL = [str(w) for w in settings.get('pages.skip_deal_words', []) or [] if str(w).strip()]
NOT_OURS = re.compile(r'\b(?:' + '|'.join(re.escape(w) for w in _SKIP_DEAL) + r')\b', re.I) if _SKIP_DEAL else re.compile(r'(?!x)x')
TEST_ROW = re.compile(r'\b(?:sample deal|test|automation test)\b', re.I)
LEGAL = re.compile(r'[,\s]+(?:inc|llc|l\.l\.c|ltd|corp|corporation|co|company|incorporated|plc|lp|llp|pllc|pc)\.?$', re.I)
BULK = 10                        # a message to more than this many people is a mass send, not a conversation
# Optional HubSpot source: the portal id the connected account must report, and the read-only Composio account.
# Both unset (the default) = HubSpot is off: nothing is read from it and pages are made from mail alone.
try:
    HUBSPOT_PORTAL = int(settings.get('pages.hubspot_portal', 0) or 0)
except (TypeError, ValueError):
    HUBSPOT_PORTAL = 0
HUBSPOT_ACCOUNT = str(settings.get('pages.hubspot_account', '') or '')
HUBSPOT_ON = bool(HUBSPOT_PORTAL and HUBSPOT_ACCOUNT)

SOURCE_RANK = {'owner': 0, 'email': 1, 'hubspot': 2}


class Refused(Exception):
    """The seed will not run as asked (no dry run seen, already seeded, a tick or another seed holds the folder)."""


# ---------- small helpers ----------

def on(life_home) -> bool:
    try:
        return (Path(life_home) / PAGES_ON).exists()
    except Exception:
        return False


def _iso(at) -> str:
    try:
        return time.strftime('%Y-%m-%d', time.localtime(float(at)))
    except (TypeError, ValueError):
        return '????-??-??'


def _ts(value) -> float | None:
    """ISO time ("YYYY-MM-DDTHH:MM:SSZ", HubSpot's "...118Z") or epoch millis -> epoch seconds."""
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            v = float(value)
            return v / 1000 if v > 1e11 else v
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def _clean(s, cap=None) -> str:
    """One plain line: no brackets, no pipes, no links, no long dashes (the chat's own plainness check would drop it)."""
    s = re.sub(r'https?://\S+|www\.\S+', '(link)', str(s or ''))
    s = re.sub(r'\s+\|\s+', ': ', s)                       # "Acme | Billing" -> "Acme: Billing"
    s = re.sub(r'[\[\]{}<>|`*_]+', ' ', s)
    s = P._plain(s)
    if cap and len(s) > cap:
        s = s[:cap].rsplit(' ', 1)[0].rstrip(',;:') + '...'
    return s


def address_key(address: str) -> str:
    """One spelling per mailbox (sources.same_mailbox): lower case, no "+tag", Gmail without dots."""
    local, _, domain = str(address or '').strip().lower().partition('@')
    local = local.split('+', 1)[0]
    if domain in ('gmail.com', 'googlemail.com'):
        local, domain = local.replace('.', ''), 'gmail.com'
    return f'{local}@{domain}' if local and domain else ''


OWN_KEYS = {address_key(a) for a in OWN}


def robot(address: str, name: str = '') -> str | None:
    """Why this address is not a person the owner deals with, or None."""
    a = address_key(address)
    local, _, domain = a.partition('@')
    if not a or '.' not in domain:
        return 'not an address'
    if a in OWN_KEYS:
        return "the owner's own address"
    if any(domain == d or domain.endswith('.' + d) for d in AGENT_DOMAINS):
        return "one of the owner's agents"
    if ROBOT_LOCAL.match(local) or ROBOT_DOMAIN.search(domain) or 'unsub' in local:   # unsubscribe robots
        return 'an automated sender'
    if name and (ROLE_NAME.search(name) or org_name(name, domain)):
        return 'a shared mailbox, not a person'
    return None


def org_name(name: str, domain: str) -> bool:
    """The display name is the organisation's own name, not a person's ("Acme Robotics" writing from
    acmerobotics.example). True when the name with its spaces taken out starts with the web
    address's own label or the other way round, 5 letters or more. Free mail hosts never count (a person may be
    called Gmail-something; nobody is their employer's whole name)."""
    label = domain.split('.')[-2] if domain.count('.') else ''
    flat = re.sub(r'[^a-z0-9]', '', LEGAL.sub('', str(name or '')).lower())
    return domain not in FREE_MAIL and len(label) >= 5 and len(flat) >= 5 and (flat.startswith(label) or label.startswith(flat))


def person_name(display: str, address: str) -> str | None:
    """The name to file a person under, from the address's display name. None when there is no real name."""
    n = re.sub(r'\s+', ' ', str(display or '').replace('"', '')).strip()
    if not n or '@' in n or not re.search(r'[^\W\d_]', n) or len(n) > 60:
        return None
    if ',' in n:                                      # "Rivera, Alex" -> "Alex Rivera"
        last, _, first = n.partition(',')
        n = f'{first.strip()} {last.strip()}'.strip()
    return P._show(n)


def _first(name: str) -> str:
    return str(name or '').split(' ')[0] or str(name or '')


def _company_aliases(name: str) -> list:
    """"J. Taylor Builders, Inc" is also written "J. Taylor Builders"; only legal endings are dropped."""
    short = LEGAL.sub('', str(name or '')).strip(' ,.')
    return [short] if short and short != name and len(short) >= 4 else []


def _norm(s) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', LEGAL.sub('', str(s or '')).lower()))


def _differ(a, b) -> bool:
    """Two claims really disagree: neither one's words are all inside the other's ("VP Ops" vs "VP Ops at Acme" agree)."""
    wa, wb = set(_norm(a).split()) - P.FILLER, set(_norm(b).split()) - P.FILLER
    return bool(wa and wb) and not (wa <= wb or wb <= wa)


# ---------- the stores ----------

def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise P.Unreadable(str(e)[:120])
    if not isinstance(d, dict):
        raise P.Unreadable(f'{path.name} is not a list of pages')
    return d


def load_sources(life_home) -> dict:
    return _read(P._dir(life_home) / 'sources.json')


def load_companies(life_home) -> dict:
    return _read(P._home(life_home) / 'companies' / 'index.json')


# One person, one record: every caller goes through one lookup (person_id), so the chat's name matching, the index
# view and the seed never keep their own joins.
OWN_DOMAINS = ({a.split('@')[1] for a in OWN if '@' in a} | {str(d).lower() for d in settings.OWN_DOMAINS}) - FREE_MAIL


def person_id(index: dict, src_all: dict, name: str = '', address: str = '') -> tuple:
    """The ONE lookup for "which person is this", over both stores (the owner's chat pages and the mail and HubSpot
    pages). -> (person id, how it was found) or (None, why not). Without it the same full name writing from a second
    address would become a second page, and each page would lack the other's address.
      address   any address already on file for that person
      name      on the owner's own chat pages: exactly one person carries it
                on a mail page: the same full name (two words or more), exactly one page has it, AND the new address is at
                the same web address as one on file or both are at the owner's own companies' addresses
    The same name anywhere else is never joined ('ask'): that is a question for the owner, never a guess."""
    k = address_key(address) if address else ''
    if k:
        for slug, e in src_all.items():
            if k in {address_key(a) for a in e.get('emails') or []}:
                return slug, 'address'
    core = P._core(name) if name else ''
    if not core:
        return None, 'unknown'
    def kept_apart(slug) -> bool:
        """The owner said this page and another are two different people, and this address sits at that other
        person's web address: the name alone must not file it here."""
        if not k:
            return False
        dom = k.split('@')[1]
        marks = set((index.get(slug) or {}).get('not_same') or {}) | set((src_all.get(slug) or {}).get('not_same') or {})
        marks |= {s for s, e in src_all.items() if P.not_same(e, slug)}
        return any(dom in {address_key(a).split('@')[1] for a in (src_all.get(o) or {}).get('emails') or []} for o in marks)

    exact = sorted(s for s, x in index.items() if core in {P._core(n) for n in P._names(x)})
    if exact:
        if len(exact) == 1 and kept_apart(exact[0]):
            return None, 'not the same person'
        return (exact[0], 'owner page') if len(exact) == 1 else (None, 'unclear')
    same = sorted(s for s, e in src_all.items() if ' ' in core and core in {P._core(n) for n in [e.get('name') or ''] + list(e.get('aliases') or [])})
    if len(same) == 1 and k:
        dom = k.split('@')[1]
        theirs = {address_key(a).split('@')[1] for a in src_all[same[0]].get('emails') or []}
        if (dom in theirs and dom not in FREE_MAIL) or (dom in OWN_DOMAINS and theirs & OWN_DOMAINS):
            return same[0], 'same name'
    return None, 'ask' if same else 'unknown'


def mark_not_same(life_home, a: str, b: str, said: str, at, source: str) -> bool:
    """Write down, on both records, that `a` and `b` are two different people, with the owner's word for it, when it
    was said and where. Inside the two existing stores (people/index.json, people/sources.json); no new file. After
    this, people.merge, people.add_alias, rekey and person_id all refuse to join them. -> False when neither is on file."""
    index, src_all = P.load(life_home), load_sources(life_home)
    mark = {'said': str(said)[:200], 'at': at, 'source': str(source)[:200]}
    hit = False
    for mine, other in ((a, b), (b, a)):
        for store in (index, src_all):
            if mine in store:
                store[mine].setdefault('not_same', {})[other] = mark
                hit = True
    if hit:
        P._write(P._dir(life_home) / 'index.json', json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True))
        P._write(P._dir(life_home) / 'sources.json', json.dumps(src_all, indent=2, ensure_ascii=False, sort_keys=True))
    return hit


def records(life_home) -> dict:
    """Person id -> ONE record made from both stores: every name, every address, the owner's own words about them
    (`person`, people.py's entry) and what mail and HubSpot say (`src`). Readers that need "who is on file" start
    here (the chat's name matching, the index view), so nothing about a person is kept or joined in a second place."""
    def read(load):
        try:
            return load(life_home)
        except P.Unreadable:
            return {}
    index, src_all = read(P.load), read(load_sources)
    out = {}
    for slug in sorted(set(index) | set(src_all)):
        person, src = index.get(slug), src_all.get(slug)
        mine = list((person or {}).get('aliases') or [])
        out[slug] = {'id': slug, 'name': (person or src or {}).get('name') or slug, 'person': person, 'src': src,
                     'aliases': mine + [a for a in (src or {}).get('aliases') or [] if a not in mine], 'emails': list((src or {}).get('emails') or [])}
    return out


def index_view(life_home, now=None) -> list:
    """One plain line per person, made from records() and nothing else: closest first (only a closeness the owner
    stated counts; everyone else sits at the default), then people on the owner's own pages, then the newest contact. No address
    is printed."""
    rows = []
    for r in records(life_home).values():
        person, src = r['person'] or {}, r['src'] or {}
        close = int(person.get('closeness') or P.DEFAULT_CLOSENESS) if (person.get('support') or {}).get('closeness') else P.DEFAULT_CLOSENESS
        top = newest_seen('person', person, src) or {}
        who_is = P._plain(person['relation']) if person.get('relation') and (person.get('support') or {}).get('relation') else f'who they are to {OWNER}: not said'
        who_is = f"one of {OWNER}'s own agents (not an outside person)" + ('' if who_is.endswith('not said') else '; ' + who_is) if P.is_agent(person) else who_is
        bits = [who_is, f"{len(P._open_threads(person, now))} open threads"] + ([f"newest source {top['date']}"] if top else [])
        rows.append(((-close, not person, -(top.get('at') or 0), r['id']), f"- {r['name']}: {'; '.join(bits)}; page people/{r['id']}.md"))
    return [line for _, line in sorted(rows)]


def _add(entry: dict, ev: dict) -> bool:
    """Append-only: a line with the same id is never added twice and never changed."""
    have = entry.setdefault('evidence', [])
    if any(e.get('id') == ev.get('id') for e in have):
        return False
    have.append(ev)
    if len(have) > EVIDENCE_KEPT:                # drop the oldest email lines only: HubSpot lines and claims stay
        mail = sorted((e for e in have if e.get('source') == 'email'), key=lambda e: e.get('at') or 0)
        for e in mail[:len(have) - EVIDENCE_KEPT]:
            have.remove(e)
    return True


def rekey(life_home, old: str, new: str) -> bool:
    """people.merge folded page `old` into `new`: what email and HubSpot said about `old` moves too."""
    src = load_sources(life_home)
    if old not in src:
        return False
    if P.not_same(src[old], new) or P.not_same(src.get(new), old):     # the owner said these are two different people
        return False
    moved = src.pop(old)
    keep = src.setdefault(new, {'name': moved.get('name'), 'aliases': [], 'emails': [], 'evidence': []})
    for e in moved.get('evidence') or []:
        _add(keep, e)
    keep['emails'] = sorted(set(keep.get('emails') or []) | set(moved.get('emails') or []))
    keep.setdefault('company', moved.get('company'))
    comps = load_companies(life_home)
    for c in comps.values():
        if old in (c.get('people') or []):
            c['people'] = sorted({new if s == old else s for s in c['people']})
    P._write(P._dir(life_home) / 'sources.json', json.dumps(src, indent=2, ensure_ascii=False, sort_keys=True))
    if comps:
        P._write(P._home(life_home) / 'companies' / 'index.json', json.dumps(comps, indent=2, ensure_ascii=False, sort_keys=True))
    return True


# ---------- what a page says ----------

def _cite(source: str, where: str, at) -> str:
    return f'{where}, {P._day(at, year=True)}'


def _lines(kind: str, person: dict | None, src: dict | None, company: dict | None = None) -> list:
    """Every dated line for one page, newest first: [{'at', 'source', 'text', 'where', 'claim'?}]. The owner's own chat
    words come from people.py's index exactly as kept there (the quote included); nothing here rewrites them."""
    out = []
    for f in (person or {}).get('facts') or []:
        out.append({'at': f.get('at'), 'source': 'owner',
                    'where': f"{OWNER}'s own words, said directly" if str(f.get('message_id')).startswith('said:') else f"{OWNER}'s own words in chat",
                    'text': f"{P._sentence(f.get('text', ''))} Their words: \"{P._plain(f.get('quote', ''))}\""})
    for t in (person or {}).get('open_threads') or []:
        out.append({'at': t.get('at'), 'source': 'owner', 'where': f"{OWNER}'s own words in chat",
                    'text': f"Still open: {P._sentence(t.get('text', ''))} Their words: \"{P._plain(t.get('quote', ''))}\""})
    for t in (person or {}).get('closed_threads') or []:      # a closed thread stays in the history, said as closed
        out.append({'at': t.get('at'), 'source': 'owner', 'where': f"{OWNER}'s own words in chat",
                    'text': f"No longer open ({P._plain((t.get('closed') or {}).get('why') or 'closed')}): {P._sentence(t.get('text', ''))} "
                            f"Their words: \"{P._plain(t.get('quote', ''))}\""})
    for e in ((src or {}).get('evidence') or []) + ((company or {}).get('evidence') or []):
        out.append(e)
    return sorted(out, key=lambda e: (-(e.get('at') or 0), SOURCE_RANK.get(e.get('source'), 9)))


def newest(kind: str, person: dict | None, src: dict | None, company: dict | None = None, sources=None) -> dict | None:
    """The newest dated source line on a page: {'at', 'date', 'iso', 'source', 'where', 'text'}. None when the page
    has no dated line. sources=('owner', 'email') leaves HubSpot out: a HubSpot line is dated by when the record last
    changed, which an import or a workflow can move without anyone talking to the owner."""
    for e in _lines(kind, person, src, company):
        if e.get('at') and (sources is None or e.get('source') in sources):
            return {'at': e['at'], 'date': P._day(e['at'], year=True), 'iso': _iso(e['at']), 'source': e.get('source'),
                    'where': e.get('where'), 'text': e.get('text')}
    return None


def newest_seen(kind, person, src, company=None) -> dict | None:
    """The newest thing actually said or written (the owner's words, an email), else the newest line of any kind. This is the
    page's "Newest source" line: a HubSpot record's date moves with every import, so it never outranks a real email."""
    return newest(kind, person, src, company, sources=('owner', 'email')) or newest(kind, person, src, company)


def _claims(evidence: list, field: str) -> list:
    """[(value, at, where)] newest first, for one field (who, company), from the email and HubSpot lines."""
    got = [((e.get('claim') or {}).get('value'), e.get('at'), e.get('where')) for e in evidence or []
           if (e.get('claim') or {}).get('field') == field and (e.get('claim') or {}).get('value')]
    return sorted(got, key=lambda c: -(c[1] or 0))


def summary(kind: str, name: str, person: dict | None, src: dict | None, company: dict | None = None,
            companies: dict | None = None) -> list:
    """The top of the page: at most three plain lines, each with its source and date, plus one "sources differ" line
    when two sources disagree. -> [{'text', 'at', 'source'}]. Code builds every line from the page's own lines, so
    each one traces to the timeline below it."""
    out = []
    person = person or {}
    sup = person.get('support') or {}
    ev = ((src or {}).get('evidence') or []) + ((company or {}).get('evidence') or [])
    said = sup.get('his') or {}
    if P.is_agent(person):                              # said first, wherever the page is shown (the page, the chat, research)
        out.append({'text': P.AGENT_LINE.format(name=name), 'at': (person.get('agent') or {}).get('at'), 'source': 'owner'})
    if person.get('his') in ('yes', 'no') and said:
        verb = f"aren't for {OWNER}" if person['his'] == 'no' else f'are for {OWNER}'
        out.append({'text': f"{OWNER} said things about {name} {verb}: \"{P._plain(said.get('quote', ''))}\" "
                            f"({_cite('owner', IN_CHAT, said.get('at'))}).", 'at': said.get('at'), 'source': 'owner'})
    differ = None
    if kind == 'person':
        who_hub = _claims(ev, 'who')
        if person.get('relation') and sup.get('relation'):
            rel = sup['relation']
            where = f"{OWNER}'s own words, said directly" if str(rel.get('message_id')).startswith('said:') else f"{OWNER}'s own words in chat"
            told = f". {P._in_his_words(rel)}" if rel.get('asked') else f", in {OWNER}'s words: \"{P._plain(rel.get('quote', ''))}\""
            out.append({'text': f"Who: {P._plain(person['relation'])}{told} "
                                f"({_cite('owner', where, rel.get('at'))}).", 'at': rel.get('at'), 'source': 'owner'})
            if who_hub and _differ(person['relation'], who_hub[0][0]):
                differ = (f"Sources differ on who {name} is: {OWNER} said \"{P._plain(rel.get('quote', ''))}\" "
                          f"({P._day(rel.get('at'), year=True)}); {who_hub[0][2]} says {who_hub[0][0]} "
                          f"({P._day(who_hub[0][1], year=True)}). {OWNER}'s words come first.")
        elif who_hub:
            out.append({'text': f"Who: HubSpot lists {name} as {who_hub[0][0]} ({_cite('hubspot', who_hub[0][2], who_hub[0][1])}).",
                        'at': who_hub[0][1], 'source': 'hubspot'})
        else:
            dom = next((e.split('@')[1] for e in (src or {}).get('emails') or [] if e.split('@')[1] not in FREE_MAIL), None)
            if dom and ev:
                first_mail = min((e for e in ev if e.get('source') == 'email'), key=lambda e: e.get('at') or 0, default=None)
                if first_mail:
                    out.append({'text': f"Who: someone {OWNER} emails at {dom} ({_cite('email', first_mail['where'], first_mail['at'])}).",
                                'at': first_mail['at'], 'source': 'email'})
        deal = next((e for e in sorted(ev, key=lambda e: -(e.get('at') or 0)) if (e.get('claim') or {}).get('field') == 'deal'), None)
        if deal:
            out.append({'text': f"{deal['text'].rstrip('.')} ({_cite('hubspot', deal['where'], deal['at'])}).", 'at': deal['at'], 'source': 'hubspot'})
        comp = _claims(ev, 'company')
        if comp and differ is None:
            names = {}
            for value, at, where in comp:
                names.setdefault(_norm(value), (value, at, where))
            if len(names) > 1:
                (a, at_a, wh_a), (b, at_b, wh_b) = list(names.values())[:2]
                differ = (f"Sources differ on where {name} works: {wh_a} says {a} ({P._day(at_a, year=True)}); "
                          f"{wh_b} says {b} ({P._day(at_b, year=True)}).")
    else:
        what = next((e for e in sorted(ev, key=lambda e: -(e.get('at') or 0)) if (e.get('claim') or {}).get('field') == 'what'), None)
        if what:
            out.append({'text': f"{what['text'].rstrip('.')} ({_cite('hubspot', what['where'], what['at'])}).", 'at': what['at'], 'source': 'hubspot'})
        deal = next((e for e in sorted(ev, key=lambda e: -(e.get('at') or 0)) if (e.get('claim') or {}).get('field') == 'deal'), None)
        if deal:
            out.append({'text': f"{deal['text'].rstrip('.')} ({_cite('hubspot', deal['where'], deal['at'])}).", 'at': deal['at'], 'source': 'hubspot'})
    used = {o['text'] for o in out}
    for e in _lines(kind, person, src, company):
        if len(out) >= SUMMARY_MAX:
            break
        if e.get('claim') or not e.get('at'):          # who / where / deal lines are said above, not as "latest"
            continue
        quote = re.search(r'Their words: "(.+)"$', str(e.get('text') or ''))
        if quote and any(f'"{quote.group(1)}"' in u for u in used):
            continue                                   # the owner's statement is already quoted above: the next line says more
        line = f"Latest: {e['text'].rstrip('.')} ({_cite(e.get('source'), e.get('where'), e.get('at'))})."
        if line not in used and not any(e['text'] in u for u in used):
            out.append({'text': line, 'at': e.get('at'), 'source': e.get('source')})
            break
    out = out[:SUMMARY_MAX]
    if differ:
        out.append({'text': differ, 'at': None, 'source': 'differ'})
    if not out:
        out.append({'text': f"Nothing dated is known about {name} yet.", 'at': None, 'source': None})
    return out


def _entry_parts(life_home, kind, slug, index=None, src_all=None, comp_all=None):
    index = P.load(life_home) if index is None else index
    src_all = load_sources(life_home) if src_all is None else src_all
    comp_all = load_companies(life_home) if comp_all is None else comp_all
    if kind == 'person':
        person, src = index.get(slug), src_all.get(slug)
        name = (person or {}).get('name') or (src or {}).get('name') or slug
        return name, person, src, None
    c = comp_all.get(slug) or {}
    return c.get('name') or slug, None, None, c


def render(kind: str, name: str, person: dict | None, src: dict | None, company: dict | None = None,
           names_of: dict | None = None) -> str:
    """The readable page: summary, the newest source, details, then <!-- timeline --> and the dated, sourced lines."""
    person = person or {}
    lines = [f'# {name}', '']
    lines += [s['text'] for s in summary(kind, name, person, src, company)]
    top = newest_seen(kind, person, src, company)
    if top:
        lines.append(f"Newest source: {top['date']}, {top['where']}.")
    lines.append('')
    aliases = list(person.get('aliases') or []) + [a for a in (src or company or {}).get('aliases') or [] if a not in (person.get('aliases') or [])]
    if aliases:
        lines.append('Also called: ' + ', '.join(aliases) + '.')
    if kind == 'person':
        if (src or {}).get('emails'):
            lines.append('Email: ' + ', '.join(src['emails']) + '.')
        if (src or {}).get('company') and names_of:
            cname = names_of.get(src['company'])
            if cname:
                lines.append(f'Company: {cname}.')
        sup = person.get('support') or {}
        if sup.get('closeness'):
            c = int(person.get('closeness') or P.DEFAULT_CLOSENESS)
            lines.append(f"How close: {c} of 5, {P.HOW_CLOSE.get(c, '')}. {OWNER}'s words: \"{P._plain(sup['closeness'].get('quote', ''))}\"")
        if person:
            his = person.get('his', 'unknown')
            if not (his in ('yes', 'no') and sup.get('his')):
                lines.append(f'Bring {OWNER} things about {name}: {OWNER} has not said either way.')
        threads = P._open_threads(person) if person else []
        if threads:
            lines.append('Still open: ' + '; '.join(P._plain(t.get('text', '')) for t in threads) + '.')
    else:
        if (company or {}).get('domains'):
            lines.append('Web: ' + ', '.join(company['domains']) + '.')
        folks = [names_of.get(s) for s in (company or {}).get('people') or [] if names_of and names_of.get(s)]
        if folks:
            lines.append('People there: ' + ', '.join(folks) + '.')
    lines += ['', SENTINEL, '', '## Timeline']
    shown = _lines(kind, person, src, company)[:TIMELINE_SHOWN]
    for e in shown:
        lines.append(f"- **{_iso(e.get('at'))}** | {e.get('text')} (Source: {_cite(e.get('source'), e.get('where'), e.get('at'))})")
    if not shown:
        lines.append('- Nothing yet.')
    return '\n'.join(lines) + '\n'


def person_page(life_home, slug: str, person: dict | None = None) -> str:
    """The page for one people/ slug in this layout. people.py calls this when pages-on is there."""
    index = P.load(life_home)
    if person is not None:
        index = {**index, slug: person}
    name, person, src, _ = _entry_parts(life_home, 'person', slug, index=index)
    comps = load_companies(life_home)
    return render('person', name, person, src, names_of={s: c.get('name') for s, c in comps.items()})


def write_pages(life_home, index=None, src_all=None, comp_all=None) -> int:
    """(Re)write every people and company page from the stores. Only pages whose text changed are written."""
    life_home = P._home(life_home)
    index = P.load(life_home) if index is None else index
    src_all = load_sources(life_home) if src_all is None else src_all
    comp_all = load_companies(life_home) if comp_all is None else comp_all
    cnames = {s: c.get('name') for s, c in comp_all.items()}
    pnames = {s: (index.get(s) or src_all.get(s) or {}).get('name') for s in set(index) | set(src_all)}
    n = 0
    # The owner's own pages (people.py's) change layout only with the switch: off, they stay exactly as people.py wrote them.
    for slug in sorted((set(src_all) - set(index)) | (set(index) if on(life_home) else set())):
        name, person, src, _ = _entry_parts(life_home, 'person', slug, index, src_all, comp_all)
        text = render('person', name, person, src, names_of=cnames)
        f = P._dir(life_home) / f'{slug}.md'
        if not f.exists() or f.read_text(encoding='utf-8') != text:
            P._write(f, text)
            n += 1
    for slug, c in comp_all.items():
        text = render('company', c.get('name') or slug, None, None, c, names_of=pnames)
        f = life_home / 'companies' / f'{slug}.md'
        if not f.exists() or f.read_text(encoding='utf-8') != text:
            P._write(f, text)
            n += 1
    return n


# ---------- who the owner's message names (for the chat) ----------

def _entities(life_home) -> dict:
    """'p:<slug>' and 'c:<slug>' -> {'kind', 'slug', 'name', 'variants', 'person', 'src', 'company', 'firsts'}."""
    try:
        comp_all = load_companies(life_home)
    except P.Unreadable:
        comp_all = {}
    out = {}
    for slug, r in records(life_home).items():           # the one record per person
        person, src, name = r['person'], r['src'], r['name']
        shape = {'name': name, 'aliases': list((person or {}).get('aliases') or []) + list((src or {}).get('aliases') or [])}
        # A bare first name finds only someone on the owner's own pages (people.py's rule). Someone known only from mail
        # or HubSpot needs their full name: "Jordan's birthday" must never bring up a Jordan once emailed about work.
        out['p:' + slug] = {'kind': 'person', 'slug': slug, 'name': name, 'variants': P._variants(shape),
                            'first': P._first(shape) if person else None, 'person': person, 'src': src, 'company': None,
                            'mail_first': None if person else P._first(shape)}     # see _match(wide=True)
    for slug, c in comp_all.items():
        shape = {'name': c.get('name') or slug, 'aliases': list(c.get('aliases') or [])}
        variants = P._variants(shape) + [d for d in c.get('domains') or [] if d]
        out['c:' + slug] = {'kind': 'company', 'slug': slug, 'name': shape['name'], 'variants': variants, 'first': None,
                            'person': None, 'src': None, 'company': c}
    return out


def _match(ents: dict, text: str, wide: bool = False) -> list:
    """Keys of the people and companies a message names, in order. A stretch of text that two pages could both mean is
    left out (never guessed); the longest name wins a stretch ("Jordan Lee" is not also "Jordan"); a one-word company
    name must be written with a capital ("Acme", not "acme"); a bare first name counts only for the owner's own pages,
    when exactly one page has it and nobody is known by that word alone, as in people.py.
    wide=True (only for "has X replied?"-type questions with contact-facts-on): a bare first name also finds someone
    known only from mail or HubSpot, under the same rules (one page has it, nobody is known by that word alone, an
    everyday-word name must be written as a name). "Did Taylor get back to me?" can only mean someone the owner writes
    to; "Jordan's birthday" is never such a question, so it still finds nobody."""
    text = str(text or '').replace('’', "'").replace('‘', "'")
    if not text.strip() or not ents:
        return []
    spans = {}
    for key, e in ents.items():
        for v in e['variants']:
            if not v:
                continue
            one_word_company = e['kind'] == 'company' and ' ' not in v.strip() and '.' not in v
            for m in P._word_re(v).finditer(text):
                if one_word_company and not text[m.start()].isupper():
                    continue
                spans.setdefault((m.start(), m.end()), set()).add(key)
    taken, found = [], {}
    for (s, end), keys in sorted(spans.items(), key=lambda kv: (-(kv[0][1] - kv[0][0]), kv[0][0])):
        if any(s < t1 and t0 < end for t0, t1 in taken):
            continue
        taken.append((s, end))
        if len(keys) == 1:
            found.setdefault(next(iter(keys)), s)
    everyone = {P._core(v) for e in ents.values() for v in e['variants']}
    firsts = {}
    for key, e in ents.items():
        f = e.get('first') or (e.get('mail_first') if wide else None)
        if f:
            firsts.setdefault(f, []).append(key)
    for first, keys in firsts.items():
        if first in everyone:
            continue
        for m in P._word_re(first).finditer(text):
            if any(m.start() < t1 and t0 < m.end() for t0, t1 in taken):
                continue
            if re.match(r"[ \t]+[A-Z][\w'-]+", text[m.end():]):
                continue
            if len(keys) == 1 and keys[0] not in found:
                found[keys[0]] = m.start()
            taken.append((m.start(), m.end()))       # a shared first name is used up either way: never guessed
            break
    return [k for k, _ in sorted(found.items(), key=lambda kv: kv[1])]


def named(life_home, text) -> list:
    """[(kind, slug, name)] for the people and companies a message clearly names. Never raises."""
    try:
        ents = _entities(Path(life_home))
        return [(ents[k]['kind'], ents[k]['slug'], ents[k]['name']) for k in _match(ents, text)]
    except Exception:
        return []


def newest_for(life_home, name) -> dict | None:
    """The freshness hook: the newest dated source line for the one person or company `name` clearly means, else None."""
    try:
        ents = _entities(Path(life_home))
        keys = _match(ents, name)
        if len(keys) != 1:
            return None
        e = ents[keys[0]]
        return newest_seen(e['kind'], e['person'], e['src'], e['company'])
    except Exception:
        return None


# ---------- how fresh it is ----------
# Why: without this, an answer about a person or deal never says how old Zeus's picture is, or that the real update may
# be in the owner's texts or calls, which Zeus cannot see, so the owner could act on a three-week-old picture. CODE works
# out the fact (the newest dated thing said or written, and whether it is old or the kind of thing settled by text or
# phone); the chat writer words it as one natural clause; the plugin's freshcheck.py then confirms any date the reply
# quotes. Switch file `freshness-on` in the life folder, which only acts together with `pages-on`. Off: nothing below
# is called.
FRESH_ON = 'freshness-on'
STALE_DAYS = 7                   # "older than about 7 days"
# A question about how things stand with someone, or whether they have been in touch. Statements ("Riley isn't for
# me", "Jordan's birthday is Saturday"), instructions and small talk never match: the clause is for status questions.
ASKS_ABOUT = re.compile(
    r"\bwhere (?:are|do|did) (?:we|things|i)\b|\bwhere(?:'s| is| are)\b.*\b(?:at|with|deal|quote|order|contract|proposal)\b"
    r"|\bwhere (?:it|that|this|things) stands?\b|\bwhat(?:'s| is| was)\b.*\b(?:story|status|latest|update|going on|happening|new|"
    r"deal|situation|up with|news)\b|\b(?:any|an|the|no) (?:word|news|update|reply|response|answer)s?\b"
    r"|\b(?:latest|status|update|news) (?:on|from|with|about|for)\b|\bhea(?:r|rd) (?:from|back)\b"
    r"|\b(?:did|has|have|had)\s+(?!you\b|u\b)[\w.'-]+(?:\s+[\w.'-]+){0,3}\s+(?:reply|replied|respond|responded|answer|answered|"
    r"get back|got back|gotten back|write back|wrote back|written back|email|emailed|text|texted|call|called|sign|signed|"
    r"pay|paid|send|sent|say|said|confirm|confirmed|decide|decided|order|ordered)\b"
    r"|\b(?:when|what) did (?:i|we) last\b|\blast (?:time )?(?:i|we) (?:heard|talked|spoke|emailed|wrote)\b"
    r"|\b(?:catch|fill) me (?:up|in)\b|\bremind me (?:about|who|what|where)\b|\btell me about\b|\bbrief me\b|\bupdate me\b"
    r"|\bwhat do (?:you|we|i) (?:know|have)\b|\bhow(?:'s| is| are)\b.*\b(?:going|doing|looking|coming|things)\b"
    r"|\bhow are things\b|\bwho(?:'s| is| was)\b|\bstatus\b|\bthe deal (?:with|for)\b", re.I)
# The owner talks about texting or calling them, or the owner's own words about them do: the update may well sit where
# Zeus can't see.
PHONE_WORDS = re.compile(r"\b(?:text(?:ed|s|ing)?|call(?:ed|s|ing)?|phone(?:d|s)?|voicemail|face ?time|imessage|whatsapp|"
                         r"rang|on the phone|spoke to|talked to)\b", re.I)


def asks_about(message) -> bool:
    """The owner's message asks how things stand with someone or something, or whether they have been in touch."""
    return bool(ASKS_ABOUT.search(str(message or '').replace('’', "'")))


def _days_ago(at, now) -> int:
    """Whole calendar days between the line's day and today, on this Mac's clock (the same clock P._day prints)."""
    a = datetime.fromtimestamp(float(at)).date()
    return (datetime.fromtimestamp(float(now)).date() - a).days


def _what_it_was(e: dict, name: str) -> str:
    """A dated line -> what it was: "Taylor's email to Alex", "Alex's email to Jordan", "what Alex told you directly",
    "Alex's HubSpot" (Alex = the owner)."""
    where = str(e.get('where') or '')
    if e.get('source') == 'owner':
        return f'what {OWNER} told you directly'
    m = re.match(r'an email from (.+?) to ' + OWNER_RE + r"'s ", where)
    if m:
        return f"{m.group(1)}'s email to {OWNER}"
    if where.startswith(f'an email {OWNER} sent'):
        return f"{OWNER}'s email to {name}"
    return f"{OWNER}'s HubSpot"


def _what_kind(e: dict) -> str:
    """What a dated line was, for code: 'told', 'owner_email', 'their_email' or 'crm' (see _what_it_was)."""
    where = str(e.get('where') or '')
    if e.get('source') == 'owner':
        return 'told'
    if where.startswith('an email from '):
        return 'their_email'
    if where.startswith(f'an email {OWNER} sent'):
        return 'owner_email'
    return 'crm'


def _fresh_lines(ents: dict, e: dict) -> list:
    """Every dated line for one page; for a company, the lines of the people there too (their emails ARE the company
    getting in touch). Newest first."""
    out = list(_lines(e['kind'], e['person'], e['src'], e['company']))
    if e['kind'] == 'company':
        for slug in (e['company'] or {}).get('people') or []:
            pe = ents.get('p:' + slug)
            if pe:
                out += [dict(x, by=pe['name']) for x in _lines('person', pe['person'], pe['src'])]
    return sorted(out, key=lambda x: (-(x.get('at') or 0), SOURCE_RANK.get(x.get('source'), 9)))


def freshness(life_home, message, now=None, question=True) -> dict | None:
    """In code: for the first person or company the owner's message clearly names (the pages section's own
    matching), how fresh Zeus's picture is. question=True (what the turn uses) also needs the message to ask how things
    stand (asks_about); the reply check passes False so a date the reply quotes is checked whatever was asked.
    -> {'name', 'kind', 'at', 'date', 'day', 'days', 'what', 'what_kind', 'to', 'direct', 'phone', 'stale', 'clause',
        'dates'} or None.
      what_kind  what the newest line was, for code: 'told' (the owner's own words), 'owner_email' (the owner's email,
                 `to` = who it went to), 'their_email', or 'crm'; `what` is the same in plain words
      direct  the newest line is the owner's own words or an email; False when only HubSpot knows them (its date moves
              with imports, so it is never "the last thing seen")
      phone   the owner asks about a text or call, or the owner's own words about them mention texting or calling, or
              they are someone the owner only ever talks to Zeus about (an owner page, no email at all)
      clause  the writer should add the one natural clause: stale (older than STALE_DAYS), no direct line, or phone
      dates   every day ("Nov 2") a dated line on the page has, which the reply check may accept beside the newest
    Nobody clearly named, or any trouble: None."""
    try:
        if question and not asks_about(message):
            return None
        life_home = Path(life_home)
        now = time.time() if now is None else float(now)
        ents = _entities(life_home)
        keys = _match(ents, str(message or '')[:2000], wide=(life_home / CONTACT_ON).exists() and contact_ask(message))
        if not keys or P.is_agent(ents[keys[0]].get('person')):     # no "your picture of them is N days old" about one of the owner's own agents
            return None
        e = ents[keys[0]]
        lines = [x for x in _fresh_lines(ents, e) if x.get('at')]
        if not lines:
            return None
        real = [x for x in lines if x.get('source') in ('owner', 'email')]
        top = (real or lines)[0]
        by = top.get('by')
        what = _what_it_was(top, e['name'])
        what_kind = _what_kind(top)
        to = e['name'] if what_kind == 'owner_email' else None
        there = re.match(OWNER_RE + r' (?:emailed|copied) (.+?) (?:there|on an email)\b', str(top.get('text') or ''))
        if what_kind == 'owner_email' and (by or (e['kind'] == 'company' and there and there.group(1) != 'someone')):
            to = by or there.group(1)
            what = f"{OWNER}'s email to {to} at {e['name']}"
        days = _days_ago(top['at'], now)
        his_words = ' '.join(str(f.get('quote', '')) for f in ((e['person'] or {}).get('facts') or []) + ((e['person'] or {}).get('open_threads') or []))
        phone = bool(PHONE_WORDS.search(str(message or ''))) or bool(PHONE_WORDS.search(his_words)) or \
            (e['kind'] == 'person' and bool(e['person']) and not any(x.get('source') == 'email' for x in lines))
        stale = days > STALE_DAYS
        return {'name': e['name'], 'kind': e['kind'], 'at': top['at'], 'date': P._day(top['at'], year=True),
                'day': P._day(top['at']), 'days': days, 'what': what, 'what_kind': what_kind, 'to': to,
                'direct': bool(real), 'phone': phone, 'stale': stale,
                'clause': stale or not real or phone, 'dates': sorted({P._day(x['at']) for x in lines})}
    except Exception:
        return None


def freshness_fact(f: dict) -> str:
    """The plain fact handed to the writer: "Newest thing you have on Taylor Reed: Taylor's email to Alex, Nov 2 (12 days ago)."."""
    ago = 'today' if f['days'] <= 0 else 'yesterday' if f['days'] == 1 else f"{f['days']} days ago"
    if not f['direct']:
        return (f"Nothing from or to {f['name']} by email or in {OWNER}'s own words; the only dated thing is "
                f"{f['what']}, {f['day']} ({ago}).")
    return f"Newest thing you have on {f['name']}: {f['what']}, {f['day']} ({ago})."


# ---------- who wrote last, what is owed, and the owner's own words ----------
# Why: a page lists what was written but not who wrote LAST, nothing from the owed-reply list, and none of the owner's
# own words, so "has Taylor replied?" could get "Not that I can see" when Taylor's email WAS the reply, or when the owner
# had already said "I already spoke to Taylor. Disregard that." So, at question time, CODE builds three plain facts for
# the one person the owner's contact question names ("has X replied?", "did X get back to me?", "any word from X?"):
#   1. who wrote last, when, and about what: the page's email lines, or the owed-reply list when it has newer;
#   2. whether a reply is owed, or already settled (loops.settled, the very test the morning list uses);
#   3. what the owner told Zeus about them lately (chats.py, read-only; only the sentences about that person, so the
#      Riley half of a Taylor-and-Riley message never lands on Taylor).
# The writer words the answer. The plugin's contactcheck.py then catches a reply that denies a message these facts show.
# Switch file contact-facts-on in the life folder, acting only with pages-on. Off: nothing below is called, and the
# name matching is unchanged.
CONTACT_ON = 'contact-facts-on'
HIS_WORDS_DAYS = 30              # the owner's own chat words about the person, this far back
HIS_WORDS_MAX = 2                # newest owner messages kept
# Capitalised words that are never another person's name: days, months, the agent and the owner themself.
NOT_A_NAME = frozenset(('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'january', 'february', 'march',
                        'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december',
                        str(settings.AGENT_NAME).lower()) | {w.lower() for w in str(settings.OWNER_NAME).split() if w.lower() != 'the'}
                       | {str(w).lower() for w in settings.get('owner.nicknames', []) or []} | {OWNER.lower()})
_ASK_VERBS = (r"replied|reply|responded|respond|answered|answer|got back|get back|gotten back|wrote back|write back|"
              r"written back|heard back|been in touch|gotten in touch|got in touch")
# A question about whether someone has been in touch with the owner. Statements and "did you ..." never match.
CONTACT_ASK = re.compile(
    r"\b(?:has|have|had|did|does)\s+(?!you\b|u\b|i\b|we\b)[\w.'-]+(?:\s+[\w.'-]+){0,3}?\s+(?:ever\s+|yet\s+|already\s+|even\s+)?"
    r"(?:" + _ASK_VERBS + r"|written|wrote|write|emailed|email|texted|text|called|call|messaged|message)\b"
    r"|\b(?:heard|hear|any word|anything|any news|any reply|any response|any answer)\s+(?:back\s+)?from\b"
    r"|\b(?:get|got|gotten|getting|write|wrote|text|texted|call|called) (?:back )?to me\b|\bback to me\b", re.I)
# The narrower kind: did they REPLY. Only here can "no" be flatly wrong (contactcheck.py).
REPLIED_ASK = re.compile(
    r"\b(?:has|have|had|did|does)\s+(?!you\b|u\b|i\b|we\b)[\w.'-]+(?:\s+[\w.'-]+){0,3}?\s+(?:ever\s+|yet\s+|already\s+|even\s+)?"
    r"(?:" + _ASK_VERBS + r")\b|\bheard back from\b|\b(?:get|got|gotten|getting) back to me\b", re.I)


def contact_ask(message) -> bool:
    return bool(CONTACT_ASK.search(str(message or '').replace('’', "'")))


def replied_ask(message) -> bool:
    return bool(REPLIED_ASK.search(str(message or '').replace('’', "'")))


class _EnginePath:
    """loops.settled imports the engine's own modules when it runs. Inside the chat plugin the engine folder is off the
    import path once this file has loaded, so it is put back for the call; every copy of it added during the call
    (loops.py adds its own on import) is taken off again, as the plugin's own loader does."""
    def __enter__(self):
        self.before = list(sys.path)
        sys.path.insert(0, str(HERE))

    def __exit__(self, *exc):
        here = HERE.resolve()
        for p in [p for p in sys.path if p not in self.before]:
            if Path(p).resolve() == here:
                sys.path.remove(p)
        while sys.path.count(str(HERE)) > self.before.count(str(HERE)):
            sys.path.remove(str(HERE))
        return False


def _mail_lines(e: dict) -> list:
    """The person's email lines, newest first: [{'at', 'theirs', 'subject', 'thread', 'box', 'mid'}]."""
    out = []
    for x in (e.get('src') or {}).get('evidence') or []:
        if x.get('source') != 'email' or not x.get('at'):
            continue
        where, ref = str(x.get('where') or ''), x.get('ref') or {}
        out.append({'at': float(x['at']), 'theirs': where.startswith('an email from '), 'subject': str(ref.get('subject') or ''),
                    'thread': ref.get('thread_id'), 'box': ref.get('mailbox'), 'mid': ref.get('message_id')})
    return sorted(out, key=lambda m: -m['at'])


def _bare(subject: str) -> str:
    return re.sub(r'^\s*((re|fwd?|aw)\s*:\s*)+', '', str(subject or ''), flags=re.I).strip().lower()


def _ledger(life_home):
    """The ledger, read-only, attached to a scratch connection. loops.settled reads the open loops and the items; when
    loops-on never made its table, an empty one is made in the scratch connection (never in the ledger), so the items
    the owner dropped or settled still count."""
    import sqlite3
    path = Path(life_home) / 'ledger.sqlite3'
    if not path.exists():
        return None
    con = sqlite3.connect('file::memory:', uri=True, timeout=0.5)
    con.row_factory = sqlite3.Row
    con.execute('ATTACH DATABASE ? AS ledger', (f'file:{path}?mode=ro',))
    have = {r[0] for r in con.execute("SELECT name FROM ledger.sqlite_master WHERE type='table'")}
    if 'opps' not in have:
        con.close()
        return None
    if 'loops' not in have:
        con.execute('CREATE TEMP TABLE loops(id TEXT PRIMARY KEY, kind TEXT, mailbox TEXT, thread_id TEXT, anchor TEXT, person TEXT, '
                    'address TEXT, subject TEXT, what TEXT, due TEXT, opened REAL, state TEXT, closed REAL, why TEXT, checked REAL, '
                    'shown INTEGER, item TEXT, payload TEXT)')
    return con


def _loop_rows(con, emails: set) -> list:
    if con is None or not emails:
        return []
    try:
        rows = con.execute("SELECT * FROM loops WHERE lower(address) IN (%s)" % ','.join('?' * len(emails)), sorted(emails)).fetchall()
    except Exception:
        return []                               # no loops table (loops-off): nothing owed is known
    return [dict(r) for r in rows]


# loops.py's closing reasons -> the plain words handed to the writer. A reason starts with who did it (the owner, by
# name or pronoun, or "they" for the other person); that subject is taken off first, so only the verb phrase is matched.
SETTLED_WORDS = (("told Zeus", f"{OWNER} told you it was handled"), ("dropped it", f"{OWNER} dropped it"),
                 ("finished it in chat", f"{OWNER} finished it in chat"), ("settled it", f"{OWNER} settled it"),
                 ("skipped it", f"{OWNER} skipped it"), ("answered in the thread", f"{OWNER} answered in the email thread"),
                 ("wrote to them since", f"{OWNER} has emailed them since"), ("passed on it", f"{OWNER} passed on it"),
                 ("answered the thread", f"{OWNER} answered in the email thread"))
_SUBJECT = re.compile(r'^(?:he|she|they|the owner|' + OWNER_RE + r')\s+', re.I)


def _why_plain(why: str) -> str:
    """A loop's closing reason, without any quote (a quote can carry someone else's half of the owner's message)."""
    w = str(why or '')
    if w.startswith('handled'):
        return 'it was handled'
    if re.match(r'^they answered (?!in the thread|the thread)', w, re.I):
        return 'they answered'                      # the other person answered the owner
    rest = _SUBJECT.sub('', w, count=1)
    for start, plain in SETTLED_WORDS:
        if rest.startswith(start):
            return plain
    return ''


def _his_words_about(msgs: list, e: dict, ents: dict, now: float) -> list:
    """The owner's own recent chat messages about this person, newest first: [{'at', 'quote'}]. Only the sentences
    that name them, plus what follows such a sentence until one names someone else ("I already spoke to Taylor.
    Disregard that." keeps both; "Riley isn't for me" stops it). A question is never the owner's word on anything."""
    first = P._first({'name': e['name']})
    others = [v for k, x in ents.items() if x is not e for v in x['variants'] + [x.get('first') or x.get('mail_first') or '']
              if v and P._core(v) not in {P._core(n) for n in e['variants']} and P._core(v) != first]
    shared = first and any((x.get('first') or x.get('mail_first')) == first or P._core(x['name']) == first
                           for x in ents.values() if x is not e)
    names = list(e['variants']) + ([first] if first and not shared else [])

    def about(sent):
        return any(P._word_re(n).search(sent) for n in names)

    def someone_else(sent):
        if any(P._word_re(v).search(sent) for v in others):
            return True                                   # another person or company with a page
        inside = re.findall(r"\b[A-Z][a-z]{2,}\b", ' '.join(sent.split()[1:]))   # a capitalised word inside the sentence
        return any(not about(w) and w.lower() not in NOT_A_NAME for w in inside)
    out = []
    for m in reversed(msgs or []):
        if m.get('who') != 'owner' or now - float(m.get('at') or 0) > HIS_WORDS_DAYS * 86400:
            continue
        sents = [x.strip() for x in re.split(r'(?<=[.!?])\s+|\n+', str(m.get('text') or '').replace('’', "'")) if x.strip()]
        keep, on = [], False
        for x in sents:
            if about(x):
                on = not x.endswith('?')
                if on:
                    keep.append(x)
            elif on and not someone_else(x) and not x.endswith('?'):
                keep.append(x)
            else:
                on = False
        quote = P._plain(' '.join(keep))[:220]
        if keep and quote not in {o['quote'] for o in out}:
            out.append({'at': float(m['at']), 'quote': quote})
        if len(out) >= HIS_WORDS_MAX:
            break
    return out


def contact(life_home, message, now=None, chat=None, question=True) -> dict | None:
    """In code: for the one person the owner's contact question names, who wrote last, what is owed or settled, and
    the owner's own recent words about them. chat = the owner's chat messages (chats.messages shape) when the caller
    has them; else they are read through loops' own reader (a sandbox without LIFE_CHAT_DB never reads the real store).
    -> {'name', 'first', 'last': {'theirs', 'at', 'day', 'days', 'subject', 'reply_to'} or None, 'owed': str, 'his': [...],
        'lines': [plain sentences]} or None (switch off, not a contact question, nobody clearly named, a company,
        any trouble)."""
    try:
        life_home = Path(life_home)
        if not ((life_home / CONTACT_ON).exists() and (life_home / PAGES_ON).exists()):
            return None
        if question and not contact_ask(message):
            return None
        now = time.time() if now is None else float(now)
        ents = _entities(life_home)
        keys = _match(ents, str(message or '')[:2000], wide=True)
        if not keys or ents[keys[0]]['kind'] != 'person' or P.is_agent(ents[keys[0]]['person']):   # an agent is never someone who owes or is owed a reply
            return None
        e = ents[keys[0]]
        first = (P._first({'name': e['name']}) or P._core(e['name'])).capitalize()
        first = next((w for w in e['name'].split() if w.lower() == first.lower()), first)
        emails = {str(a).lower() for a in (e.get('src') or {}).get('emails') or []}
        mail = _mail_lines(e)
        import sqlite3
        con = None
        try:
            con = _ledger(life_home)
        except sqlite3.Error:
            con = None
        try:
            rows = _loop_rows(con, emails)
            # the owed-reply list may have read the thread more recently than the page
            for r in rows:
                pl = json.loads(r.get('payload') or '{}')
                at = float(pl.get('last_at') or r.get('opened') or 0)
                if at and r['kind'] in ('owe_reply', 'waiting') and (not mail or at > mail[0]['at'] + 60):
                    mail.insert(0, {'at': at, 'theirs': r['kind'] == 'owe_reply', 'subject': str(r.get('subject') or ''),
                                    'thread': r.get('thread_id'), 'box': r.get('mailbox'), 'mid': r.get('anchor')})
                    mail.sort(key=lambda m: -m['at'])
            top = mail[0] if mail else None
            last = None
            if top:
                days = _days_ago(top['at'], now)
                ago = 'today' if days <= 0 else 'yesterday' if days == 1 else f'{days} days ago'
                his_before = next((m for m in mail if not m['theirs'] and m['at'] < top['at']), None)
                reply_to = None
                if top['theirs'] and his_before and (_bare(his_before['subject']) == _bare(top['subject']) or
                                                     (his_before.get('thread') and his_before.get('thread') == top.get('thread'))):
                    reply_to = P._day(his_before['at'])
                last = {'theirs': top['theirs'], 'at': top['at'], 'day': P._day(top['at']), 'days': days, 'ago': ago,
                        'subject': _clean(top['subject'], 120), 'reply_to': reply_to}
            # owed or settled
            owed = ''
            live = sorted((r for r in rows if r['state'] == 'open'), key=lambda r: -(r.get('opened') or 0))
            done = sorted((r for r in rows if r['state'] == 'closed' and now - float(r.get('closed') or 0) <= HIS_WORDS_DAYS * 86400),
                          key=lambda r: -(r.get('closed') or 0))
            if live:
                r = live[0]
                what = _clean(r.get('what'), 160)
                if r['kind'] == 'owe_reply':
                    owed = f'{OWNER} still owes {first} a reply by email: {first} asked "{what}" ({P._day(r["opened"])}).'
                elif r['kind'] == 'waiting':
                    owed = f'{first} still owes {OWNER} an answer: {OWNER} asked "{what}" ({P._day(r["opened"])}).'
                else:
                    owed = f'{OWNER} promised {first}: {what} ({P._day(r["opened"])}).'
            elif done and _why_plain(done[0].get('why')):
                owed = f'Nothing is waiting between {OWNER} and {first} now: {_why_plain(done[0].get("why"))}.'
            elif last and last['theirs'] and con is not None:
                lp = {'id': 'contact-check', 'kind': 'owe_reply', 'person': e['name'], 'address': sorted(emails)[0] if emails else '',
                      'mailbox': top.get('box'), 'thread_id': top.get('thread'), 'anchor': top.get('mid'), 'mids': [top.get('mid')],
                      'last_at': top['at'], 'opened': top['at']}
                with _EnginePath():
                    import loops as L
                    try:
                        why = L.settled(con, life_home, now, [lp]).get('contact-check')
                    except Exception:
                        why = None
                if _why_plain(why):
                    owed = f'Nothing is waiting between {OWNER} and {first} now: {_why_plain(why)}.'
        finally:
            if con is not None:
                con.close()
        if chat is None:
            with _EnginePath():
                import loops as L
                chat = L._chat(life_home, now - HIS_WORDS_DAYS * 86400)
        his = _his_words_about(chat, e, ents, now)
        lines = []
        if last and last['theirs']:
            about = f', "{last["subject"]}"' if last['subject'] else ''
            lines.append(f"{first} wrote last: {first}'s email to {OWNER} on {last['day']} ({last['ago']}){about}"
                         + (f", which is {first}'s reply to {OWNER}'s own {last['reply_to']} email." if last['reply_to'] else '.')
                         + ' Nothing newer either way by email, as far as you can see.')
        elif last:
            about = f', "{last["subject"]}"' if last['subject'] else ''
            lines.append(f"{OWNER} wrote last: {OWNER}'s email to {first} on {last['day']} ({last['ago']}){about}. "
                         f"Nothing back from {first} by email since, as far as you can see.")
        else:
            lines.append(f'Nothing from {first} by email yet, and no email from {OWNER} to {first}, as far as you can see.')
        if owed:
            lines.append(owed)
        for h in his:
            lines.append(f'On {P._day(h["at"])} {OWNER} told you: "{h["quote"]}"')
        return {'name': e['name'], 'first': first, 'last': last, 'owed': owed, 'his': his, 'lines': lines}
    except Exception:
        return None


def chat_units(life_home, message: str, limit: int = 3) -> list:
    """What the chat is handed for each person or company the owner's message names: [[opening line, more lines ...], ...].
    Plain sentences with dates and sources, no field names. Nobody clearly named, or any trouble: []."""
    try:
        life_home = Path(life_home)
        ents = _entities(life_home)
        out = []
        wide = (life_home / CONTACT_ON).exists() and contact_ask(message)       # "did Taylor get back to me?"
        for key in _match(ents, message, wide=wide)[:limit]:
            e = ents[key]
            label = e['name'] + (' (a company)' if e['kind'] == 'company' else '')
            lines = [s['text'] for s in summary(e['kind'], e['name'], e['person'], e['src'], e['company'])
                     if not s['text'].startswith('Nothing dated')]
            top = newest_seen(e['kind'], e['person'], e['src'], e['company'])
            if e['kind'] == 'company' and (life_home / FRESH_ON).exists():
                # a company's newest thing includes its people's emails, so this line and the freshness fact agree
                both = [x for x in _fresh_lines(ents, e) if x.get('at')]
                best = ([x for x in both if x.get('source') in ('owner', 'email')] or both or [None])[0]
                if best and (not top or best['at'] > top['at']):
                    top = {'date': P._day(best['at'], year=True), 'where': best.get('where'), 'at': best['at']}
            if not lines:
                continue
            unit = [f"{label}: {lines[0]}"] + [f"  {x}" for x in lines[1:]]
            if top:
                unit.append(f"  Newest thing you have on {e['name']}: {top['date']}, {top['where']}.")
            unit += [f'  {x}' for x in fresh_partial(life_home)]   # switch file pages-fresh-on; without it nothing is added
            out.append(unit)
        return out
    except Exception:
        return []


# ---------- the daily pass could not read something ----------
# pagesfresh.py folds new mail and HubSpot changes into these pages once a day. When it could not read a source it
# writes one plain sentence into its state file ("Mail in Alex's work inbox could not be read on Nov 15, so this is
# partial."). The chat is handed that sentence with the page, so "could not read" is never passed off as "nothing
# new". Switch file pages-fresh-on in the life folder: without it this returns [] and reads nothing.
FRESH_SWITCH = 'pages-fresh-on'
FRESH_STALE_AFTER = 2 * 86400     # no good daily read for this long is said too


def fresh_partial(life_home, now=None) -> list:
    """The plain "this is partial" sentences the daily pass left (at most 3), or one sentence when it has not had a
    good read for two days. [] with the switch off, with nothing missing, or on any trouble."""
    try:
        life_home = Path(life_home)
        if not (life_home / FRESH_SWITCH).exists():
            return []
        st = json.loads((life_home / 'pages-fresh.json').read_text(encoding='utf-8'))
        out = [_clean(x, 200) for x in st.get('partial') or [] if isinstance(x, str)][:3]
        good = st.get('last_good')
        now = time.time() if now is None else float(now)
        if not out and good and now - float(good) > FRESH_STALE_AFTER:
            if 'hubspot' in st.get('paused_sources', {}):
                out.append(f"Mail was last refreshed on {P._day(good)}, so this may be out of date.")
            else:
                out.append(f"New mail and HubSpot changes were last added on {P._day(good)}, so this may be out of date.")
        return out
    except Exception:
        return []


# ---------- the one-time seed ----------

# writingstyle.HTML_CUTS: where quoted history starts in an HTML message (Gmail, Apple Mail, Outlook, Yahoo)
HTML_CUTS = re.compile(r'<(?:blockquote|div[^>]+class="[^"]*(?:gmail_quote|gmail_attr|yahoo_quoted|moz-cite-prefix|OutlookMessageHeader)[^"]*"|div[^>]+id="(?:divRplyFwdMsg|appendonsend)"|hr\b)', re.I)


def _html_to_text(raw: str) -> str:
    """Some sent copies come back as raw HTML (Apple Mail): the owner's words without the markup and the quoted history."""
    import html
    m = HTML_CUTS.search(raw)
    raw = raw[:m.start()] if m else raw
    raw = re.sub(r'(?is)<(head|style|script)\b.*?</\1>', ' ', raw)
    raw = re.sub(r'(?i)<br\s*/?>|</(?:div|p|li|tr)>', '\n', raw)
    return html.unescape(re.sub(r'<[^>]+>', ' ', raw))


def _his_words(body: str, cap=160) -> str:
    """The first words written in a message: quoted history, forwarded blocks and the signature cut off."""
    text = str(body or '').replace('\r\n', '\n')
    if re.search(r'<(?:html|body|div|p|br|span|meta)\b', text, re.I):
        text = _html_to_text(text)
    for cut in (r'\nOn .{0,200}?wrote:', r'\n-{2,}\s*Original Message', r'\n-{2,}\s*Forwarded message', r'\nFrom: .*\nSent: ',
                r'\n-- ?\n', r'\n_{5,}', r'\nSent from my '):
        m = re.search(cut, text, re.I | re.S)
        if m:
            text = text[:m.start()]
    text = '\n'.join(ln for ln in text.split('\n') if not ln.lstrip().startswith('>'))
    return _clean(text, cap)


def _subject(s) -> str:
    s = _clean(re.sub(r'^\s*\[external\]\s*', '', str(s or ''), flags=re.I), 90)
    return s or '(no subject)'


def _addresses(*fields) -> list:
    return [(n, a) for n, a in email.utils.getaddresses([str(f or '') for f in fields if f]) if a and '@' in a]


def why_not_message(m: dict) -> str | None:
    """Why a sent message is left out of the seed, or None. The same rule picks the addresses checked for replies."""
    if m.get('automated'):
        return 'sent by an automation or a mailing list, not by the owner'
    if len([a for _, a in _addresses(m.get('to'), m.get('cc')) if address_key(a) not in OWN_KEYS]) > BULK:
        return 'mass send to more than %d people' % BULK
    return None


def why_not_deal(d: dict, stages: dict) -> str | None:
    p = d.get('properties') or {}
    if NOT_OURS.search(str(p.get('dealname'))) or NOT_OURS.search(str(stages.get(str(p.get('pipeline'))) or '')):
        return 'a deal handled by someone else (pages.skip_deal_words)'
    if TEST_ROW.search(str(p.get('dealname'))):
        return 'HubSpot test or sample deal'
    return None


def _deal_line(d: dict, stages: dict, now: float) -> dict:
    """One HubSpot deal as a dated, sourced line: its name, stage, amount and close date."""
    dp = d.get('properties') or {}
    stage = stages.get(str(dp.get('dealstage'))) or str(dp.get('dealstage') or '')
    amount = ''
    try:
        amount = f", ${float(dp['amount']):,.0f}" if dp.get('amount') else ''
    except (TypeError, ValueError):
        pass
    close = _ts(dp.get('closedate'))
    text = f'Deal "{_clean(dp.get("dealname"), 80)}" is at "{_clean(stage, 40)}"{amount}' + \
           (f', close date {P._day(close, year=True)}' if close else '') + '.'
    return {'id': f"hubspot:deal:{d.get('id')}:{_norm(stage)}", 'at': _ts(dp.get('hs_lastmodifieddate')) or now,
            'source': 'hubspot', 'where': 'the HubSpot deal record', 'text': text, 'claim': {'field': 'deal', 'value': stage},
            'ref': {'object': 'deals', 'id': str(d.get('id')), 'property': 'dealname', 'value': dp.get('dealname')}, 'read_at': now}


def plan(mail_by_box: dict, heard: dict, hub: dict, index: dict, existing: dict, existing_comp: dict, now: float,
         caps: dict) -> dict:
    """Pure: what the seed would file, from what was read. -> {'people', 'companies', 'skipped', 'maybe_same', 'counts'}.
    mail_by_box: mailbox -> [sent message]; heard: address key -> newest message from them; hub: the HubSpot reads."""
    skipped = {}

    def skip(why, n=1):
        skipped[why] = skipped.get(why, 0) + n

    cand = {}                                    # address -> what the owner sent them
    for box, msgs in mail_by_box.items():
        for m in msgs:
            why = why_not_message(m)
            if why:
                skip(why)
                continue
            people = [(n, a) for n, a in _addresses(m.get('to'), m.get('cc')) if address_key(a) not in OWN_KEYS]
            to = {address_key(a) for _, a in _addresses(m.get('to'))}
            for n, a in people:
                why = robot(a, n)
                if why:
                    skip(why)
                    continue
                k = address_key(a)
                c = cand.setdefault(k, {'address': a.lower(), 'names': [], 'msgs': [], 'box': box})
                if n:
                    c['names'].append(n)
                c['msgs'].append({**m, 'mailbox': box, 'others': len(people) - 1, 'copied': k not in to})
    hub = hub or {}
    stages = hub.get('stages') or {}
    contacts = {address_key(c.get('properties', {}).get('email')): c for c in hub.get('contacts') or []
                if (c.get('properties') or {}).get('email')}
    real_deals = []
    for d in hub.get('deals') or []:
        why = why_not_deal(d, stages)
        if why:
            skip(why)
        else:
            real_deals.append(d)
    deal_contacts = {str(cid) for d in real_deals for cid in d.get('contacts') or []}
    by_id = {str(c.get('id')): c for c in (hub.get('contacts') or []) + (hub.get('deal_contacts') or [])}
    for cid in sorted(deal_contacts):              # someone on a real deal is someone the owner deals with, emailed or not
        hc = by_id.get(cid)
        addr = str(((hc or {}).get('properties') or {}).get('email') or '').lower()
        if hc and addr and address_key(addr) not in cand and not robot(addr):
            cand[address_key(addr)] = {'address': addr, 'names': [], 'msgs': [], 'box': None}
            contacts.setdefault(address_key(addr), hc)
    people, maybe = {}, []
    for k, c in cand.items():
        hc = contacts.get(k)
        on_deal = bool(hc and str(hc.get('id')) in deal_contacts)
        if k not in heard and not on_deal:
            skip('one-way: never wrote back in the window')
            continue
        hp = (hc or {}).get('properties') or {}
        name = next((person_name(n, c['address']) for n in reversed(c['names']) if person_name(n, c['address'])), None)
        if not name and (hp.get('firstname') or hp.get('lastname')):
            name = person_name(f"{hp.get('firstname') or ''} {hp.get('lastname') or ''}", c['address'])
        if not name:
            skip('no name to file them under')
            continue
        people[k] = {'name': name, 'cand': c, 'hub': hc, 'heard': heard.get(k),
                     'why': 'wrote back' if k in heard else 'on a real HubSpot deal'}
    ranked = sorted(people.items(), key=lambda kv: -max([m.get('at') or 0 for m in kv[1]['cand']['msgs']] +
                                                        [(kv[1]['heard'] or {}).get('at') or 0]))
    if len(ranked) > caps.get('people', 120):
        skip('over the people cap', len(ranked) - caps.get('people', 120))
        ranked = ranked[:caps.get('people', 120)]

    # companies: HubSpot companies at the domains of the people kept, those people's own HubSpot company, real deals
    hub_comp = {str(c.get('id')): c for c in hub.get('companies') or []}
    by_domain = {}
    for cid, c in hub_comp.items():
        dom = str((c.get('properties') or {}).get('domain') or '').lower().removeprefix('www.')
        if dom:
            by_domain.setdefault(dom, []).append(cid)
    comp_out, refused = {}, set()

    def company(cid):
        c = hub_comp.get(str(cid))
        if not c or str(cid) in refused:
            return None
        p = c.get('properties') or {}
        if (not re.search(r'[^\W_]', _clean(p.get('name'))) or TEST_ROW.search(str(p.get('name')))
                or str(p.get('domain') or '') == 'hubspot.com'):
            refused.add(str(cid))
            skip('company not filed (test or no name)')
            return None
        return comp_out.setdefault(str(cid), {'hub': c, 'people': [], 'deals': [], 'mails': []})

    for k, pp in ranked:
        dom = k.split('@')[1]
        hp = (pp['hub'] or {}).get('properties') or {}
        cids = []
        if hp.get('associatedcompanyid'):
            cids.append(str(hp['associatedcompanyid']))
        if dom not in FREE_MAIL:
            cids += [x for x in by_domain.get(dom, []) if x not in cids]
        pp['companies'] = []
        for cid in cids[:2]:
            c = company(cid)
            if c is not None:
                c['people'].append(k)
                c['mails'] += pp['cand']['msgs']
                pp['companies'].append(cid)
    for d in real_deals:
        homes = list(d.get('companies') or [])
        if not homes:                              # deals often link only to a contact: file it at that contact's company
            for xid in d.get('contacts') or []:
                hp = (by_id.get(str(xid)) or {}).get('properties') or {}
                dom = str(hp.get('email') or '').lower().partition('@')[2]
                if hp.get('associatedcompanyid') and str(hp['associatedcompanyid']) in hub_comp:
                    homes.append(str(hp['associatedcompanyid']))
                elif dom and dom not in FREE_MAIL and by_domain.get(dom):
                    homes.append(by_domain[dom][0])        # the company already filed at that web address
                elif hp.get('company'):
                    key = 'name:' + _norm(hp['company'])
                    hub_comp.setdefault(key, {'id': key, 'properties': {
                        'name': hp['company'], 'domain': dom if dom and dom not in FREE_MAIL else '',
                        'hs_lastmodifieddate': hp.get('lastmodifieddate')}, 'from_contact': str(xid)})
                    homes.append(key)
                k = address_key(hp.get('email'))
                if k in people and homes:
                    people[k].setdefault('deals', []).append(d)
        for cid in homes:
            c = company(cid)
            if c is not None:
                c['deals'].append(d)
                for xid in d.get('contacts') or []:
                    k = address_key(((by_id.get(str(xid)) or {}).get('properties') or {}).get('email'))
                    if k in people and k not in c['people']:
                        c['people'].append(k)
                        c['mails'] += people[k]['cand']['msgs']
                        people[k].setdefault('companies', []).append(cid)
    if len(comp_out) > caps.get('companies', 80):
        keep = sorted(comp_out, key=lambda cid: (-len(comp_out[cid]['deals']), -len(comp_out[cid]['people'])))[:caps.get('companies', 80)]
        skip('over the company cap', len(comp_out) - len(keep))
        comp_out = {cid: comp_out[cid] for cid in keep}

    # turn it into store entries (slugs, joins with the owner's own pages, sourced lines)
    taken = set(index) | set(existing)
    by_addr = {e: s for s, entry in existing.items() for e in entry.get('emails') or []}
    src_new, comp_new = {}, {}
    comp_taken = set(existing_comp)
    comp_slug = {}
    for cid, c in comp_out.items():
        p = c['hub'].get('properties') or {}
        old = next((s for s, x in existing_comp.items() if str(x.get('hubspot_id')) == cid), None)
        slug = old or P._slug(P._show(p['name']), comp_taken)
        comp_taken.add(slug)
        comp_slug[cid] = slug
    for k, pp in ranked:
        name = pp['name']
        slug = by_addr.get(pp['cand']['address'])
        if not slug:                                # person_id() is the one lookup (address on any page, then name)
            slug, how = person_id(index, {**existing, **src_new}, name, pp['cand']['address'])
            if how in ('ask', 'not the same person'):   # the same name at an unrelated address, or people the owner said are two: never joined
                maybe.append({'seeded': name, 'same_name_page': True, 'why': how})
        if not slug:
            slug = P._slug(name, taken)
            for s, x in index.items():          # same first name as one of the owner's one-word pages: never joined, only told
                if ' ' not in P._core(x.get('name')) and P._core(x.get('name')) == P._core(_first(name)):
                    maybe.append({'seeded': name, 'his_page': x.get('name')})
        taken.add(slug)
        entry = src_new.setdefault(slug, {'name': (index.get(slug) or {}).get('name') or name, 'aliases': [], 'emails': [],
                                          'evidence': [], 'joined': slug in index})
        if pp['cand']['address'] not in entry['emails']:
            entry['emails'].append(pp['cand']['address'])
        if pp.get('companies'):
            entry['company'] = comp_slug.get(pp['companies'][0])
        first = _first(entry['name'])
        for m in sorted(pp['cand']['msgs'], key=lambda m: -(m.get('at') or 0))[:8]:
            words = '' if m.get('copied') else _his_words(m.get('body'))      # copied: the words were for someone else
            others = f' and {m["others"]} other{"s" if m["others"] > 1 else ""}' if m.get('others') else ''
            if m.get('copied'):
                text = f'{OWNER} copied {first} on an email about "{_subject(m.get("subject"))}".'
            else:
                text = f'{OWNER} emailed {first}{others} about "{_subject(m.get("subject"))}"' + (f': "{words}"' if words else '')
                text += '' if text.endswith(('."', '!"', '?"', '..."')) else '.'
            _add(entry, {'id': f"mail:{m['mailbox']}:{m['id']}", 'at': m.get('at'), 'source': 'email',
                         'where': sent_from(m['mailbox']), 'text': text,
                         'ref': {'mailbox': m['mailbox'], 'message_id': m['id'], 'thread_id': m.get('thread'),
                                 'subject': m.get('subject') or '', 'words': words}, 'read_at': now})
        h = pp.get('heard')
        if h:
            _add(entry, {'id': f"mail:{h['mailbox']}:{h['id']}", 'at': h.get('at'), 'source': 'email',
                         'where': sent_to(first, h['mailbox']),
                         'text': f'{first} wrote to {OWNER} about "{_subject(h.get("subject"))}".',
                         'ref': {'mailbox': h['mailbox'], 'message_id': h['id'], 'subject': h.get('subject') or ''}, 'read_at': now})
        for d in pp.get('deals') or []:
            _add(entry, _deal_line(d, stages, now))
        hc = pp.get('hub')
        if hc:
            hp = hc.get('properties') or {}
            title, cname = _clean(hp.get('jobtitle'), 60), _clean(hp.get('company'), 60)
            at = _ts(hp.get('lastmodifieddate')) or now
            if title:
                who = title + (f' at {cname}' if cname else '')
                _add(entry, {'id': f"hubspot:contact:{hc.get('id')}:who:{_norm(who)}", 'at': at, 'source': 'hubspot',
                             'where': "the HubSpot contact", 'text': f'HubSpot has {entry["name"]} as {who}.',
                             'claim': {'field': 'who', 'value': who}, 'ref': {'object': 'contacts', 'id': str(hc.get('id')),
                                                                              'property': 'jobtitle', 'value': hp.get('jobtitle')}, 'read_at': now})
            if cname:
                _add(entry, {'id': f"hubspot:contact:{hc.get('id')}:company:{_norm(cname)}", 'at': at, 'source': 'hubspot',
                             'where': "the HubSpot contact record", 'text': f'HubSpot lists {entry["name"]} at {cname}.',
                             'claim': {'field': 'company', 'value': cname},
                             'ref': {'object': 'contacts', 'id': str(hc.get('id')), 'property': 'company', 'value': hp.get('company')},
                             'read_at': now})
            for cid in pp.get('companies') or []:
                cp = (hub_comp.get(cid) or {}).get('properties') or {}
                if cp.get('name') and cname and _differ(cp['name'], cname):
                    _add(entry, {'id': f"hubspot:company:{cid}:people:{k}", 'at': _ts(cp.get('hs_lastmodifieddate')) or now,
                                 'source': 'hubspot', 'where': "the HubSpot company record",
                                 'text': f'HubSpot\'s company {cp["name"]} has {entry["name"]}\'s web address.',
                                 'claim': {'field': 'company', 'value': cp['name']},
                                 'ref': {'object': 'companies', 'id': cid, 'property': 'name', 'value': cp['name']}, 'read_at': now})
    for cid, c in comp_out.items():
        p = c['hub'].get('properties') or {}
        slug = comp_slug[cid]
        entry = comp_new.setdefault(slug, {'name': _clean(p['name'], 80), 'aliases': _company_aliases(_clean(p['name'], 80)),
                                           'domains': [], 'hubspot_id': cid, 'people': [], 'evidence': []})
        dom = str(p.get('domain') or '').lower().removeprefix('www.')
        if dom and dom not in entry['domains']:
            entry['domains'].append(dom)
        for k in c['people']:
            s = next((s for s, x in src_new.items() if k in [address_key(e) for e in x['emails']]), None)
            if s and s not in entry['people']:
                entry['people'].append(s)
        bits = [x for x in (_clean(p.get('industry'), 40).replace('_', ' ').lower(), ', '.join(filter(None, (_clean(p.get('city'), 30), _clean(p.get('state'), 20)))))
                if x]
        stage = str(p.get('lifecyclestage') or '')
        what = f"HubSpot lists {entry['name']}" + (f" ({dom})" if dom else '') + (': ' + '; '.join(bits) if bits else '') + \
               (f"; stage {stage}" if stage.isalpha() else '') + '.'
        via = c['hub'].get('from_contact')          # known only as the company on a deal contact's record
        _add(entry, {'id': f'hubspot:company:{cid}:what', 'at': _ts(p.get('hs_lastmodifieddate')) or now, 'source': 'hubspot',
                     'where': 'the HubSpot contact record' if via else 'the HubSpot company', 'text': what,
                     'claim': {'field': 'what', 'value': entry['name']},
                     'ref': {'object': 'contacts', 'id': via, 'property': 'company', 'value': p.get('name')} if via else
                            {'object': 'companies', 'id': cid, 'property': 'name', 'value': p.get('name')}, 'read_at': now})
        for d in c['deals']:
            _add(entry, _deal_line(d, stages, now))
        seen_threads = set()
        for m in [m for m in sorted(c['mails'], key=lambda m: -(m.get('at') or 0))
                  if not (m.get('thread') in seen_threads or seen_threads.add(m.get('thread')))][:3]:     # one line per thread
            who = next((x['name'] for s, x in src_new.items() if s in entry['people']
                        and any(address_key(a) in [address_key(e) for e in x['emails']] for _, a in _addresses(m.get('to'), m.get('cc')))), 'someone')
            _add(entry, {'id': f"mail:{m['mailbox']}:{m['id']}", 'at': m.get('at'), 'source': 'email',
                         'where': sent_from(m['mailbox']),
                         'text': f'{OWNER} emailed {who} there about "{_subject(m.get("subject"))}".',
                         'ref': {'mailbox': m['mailbox'], 'message_id': m['id'], 'subject': m.get('subject') or ''}, 'read_at': now})
    filed = [{'page': f'people/{s}', 'name': e['name'], 'why': next((pp['why'] for k, pp in ranked if pp['cand']['address'] in e['emails']), '')}
             for s, e in src_new.items()] + [{'page': f'companies/{s}', 'name': c['name'],
                                               'why': 'on a real HubSpot deal' if any(e.get('claim', {}).get('field') == 'deal' for e in c['evidence'])
                                               else 'where people the owner writes with work'} for s, c in comp_new.items()]
    counts = {'sent_messages_read': sum(len(v) for v in mail_by_box.values()), 'addresses_seen': len(cand),
              'people': len(src_new), 'people_joined_to_owner_pages': sum(1 for e in src_new.values() if e.get('joined')),
              'companies': len(comp_new), 'deals_filed': sum(len(c['deals']) for c in comp_out.values())}
    return {'people': src_new, 'companies': comp_new, 'skipped': skipped, 'maybe_same': maybe, 'counts': counts, 'filed': filed}


def _lock(life_home: Path):
    """The tick's own lock (tick.acquire): people.update runs inside the tick, so the seed never writes beside it."""
    import fcntl
    fh = open(life_home / 'tick.lock', 'a+')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(time.time()))
    fh.flush()
    return fh


def seed(life_home, mail=None, hub=None, dry=True, checked=False, again=False, days=90, now=None, caps=None,
         sample=5) -> dict:
    """The one-time seed. dry=True reads and writes NOTHING; it reports counts and `sample` sample pages. A real run
    needs checked=True (the dry run's sample was looked at) and refuses a second time unless again=True.
    mail / hub: readers (Mail, Hub below, or fakes in tests). Reads only."""
    life_home = P._home(life_home)
    now = now or time.time()
    caps = {'per_mailbox': 150, 'people': 120, 'companies': 80, **(caps or {})}
    days = max(30, min(90, int(days)))           # the seed reads the last 30 to 90 days, never more
    marker = life_home / 'pages-seeded.json'
    if not dry and not checked:
        raise Refused('run the dry run first, look at its 5 sample pages, then run again with --checked')
    if not dry and marker.exists() and not again:
        raise Refused('the seed already ran once (pages-seeded.json); give --again to run it a second time')
    index = P.load(life_home)
    existing, existing_comp = load_sources(life_home), load_companies(life_home)
    mail = mail or Mail()
    hub = hub or (Hub() if HUBSPOT_ON else NoHub())
    mail_by_box = {}
    for box in BOXES:
        mail_by_box[box] = mail.sent(box, days, caps['per_mailbox'])
    to_check = {}                                # mailbox -> the people the owner wrote to from it (same filters as plan)
    for box, msgs in mail_by_box.items():
        for m in msgs:
            if why_not_message(m):
                continue
            for n, a in _addresses(m.get('to'), m.get('cc')):
                if not robot(a, n):
                    to_check.setdefault(box, set()).add(a.lower())
    heard = {}
    for box, addrs in to_check.items():
        for a, h in mail.heard_from(box, sorted(addrs), days).items():
            k = address_key(a)
            if k not in heard or (h.get('at') or 0) > (heard[k].get('at') or 0):
                heard[k] = {**h, 'mailbox': box}
    hub_data = hub.read(sorted({a for addrs in to_check.values() for a in addrs}))
    got = plan(mail_by_box, heard, hub_data, index, existing, existing_comp, now, caps)
    src_all = {**existing}
    for slug, e in got['people'].items():
        keep = src_all.setdefault(slug, {k: v for k, v in e.items() if k != 'evidence'} | {'evidence': []})
        keep['emails'] = sorted(set(keep.get('emails') or []) | set(e['emails']))
        if e.get('company'):
            keep['company'] = e['company']
        for ev in e['evidence']:
            _add(keep, ev)
    comp_all = {**existing_comp}
    for slug, c in got['companies'].items():
        keep = comp_all.setdefault(slug, {k: v for k, v in c.items() if k != 'evidence'} | {'evidence': []})
        keep['domains'] = sorted(set(keep.get('domains') or []) | set(c['domains']))
        keep['people'] = sorted(set(keep.get('people') or []) | set(c['people']))
        for ev in c['evidence']:
            _add(keep, ev)
    reads = {'gmail_calls': getattr(mail, 'calls', None), 'hubspot_calls': getattr(hub, 'calls', None),
             'addresses_checked_for_replies': sum(len(v) for v in to_check.values())}
    report = {'dry': bool(dry), 'days': days, 'at': now, 'counts': got['counts'], 'reads': reads, 'skipped': got['skipped'],
              'maybe_same_person_not_joined': got['maybe_same'], 'filed': got['filed'], 'samples': []}
    cnames = {s: c.get('name') for s, c in comp_all.items()}
    pnames = {s: (index.get(s) or src_all.get(s) or {}).get('name') for s in set(index) | set(src_all)}
    order = sorted(got['people'], key=lambda s: -(newest('person', index.get(s), src_all[s]) or {}).get('at', 0))
    picks = [('company', s) for s in list(got['companies'])[:2]]                     # up to 2 companies, the rest people
    picks = [('person', s) for s in order[:max(0, sample - len(picks))]] + picks
    for kind, slug in picks[:sample]:
        if kind == 'person':
            text = render('person', src_all[slug]['name'] if slug not in index else index[slug]['name'], index.get(slug), src_all[slug], names_of=cnames)
            ev = src_all[slug]['evidence']
        else:
            text = render('company', comp_all[slug]['name'], None, None, comp_all[slug], names_of=pnames)
            ev = comp_all[slug]['evidence']
        report['samples'].append({'kind': kind, 'slug': slug, 'page': text,
                                  'sources': sorted({e.get('where') for e in ev})})
    if dry:
        return report
    lock = _lock(life_home)
    if lock is None:
        raise Refused('a tick is running right now; try again in a minute')
    try:
        __import__('claims').pages(life_home, src_all, comp_all)   # claims.py (switch claims-on): each sourced line says who it is from and how it was read; without the switch nothing is added
        P._write(P._dir(life_home) / 'sources.json', json.dumps(src_all, indent=2, ensure_ascii=False, sort_keys=True))
        P._write(life_home / 'companies' / 'index.json', json.dumps(comp_all, indent=2, ensure_ascii=False, sort_keys=True))
        report['pages_written'] = write_pages(life_home, index, src_all, comp_all)
        P._write(marker, json.dumps({k: report[k] for k in ('at', 'days', 'counts', 'reads', 'skipped')}, indent=2))
        with open(life_home / 'pages-seed.log.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({k: report[k] for k in ('at', 'days', 'counts', 'reads', 'skipped')}) + '\n')
    finally:
        lock.close()
    return report


def check(life_home, mail=None, hub=None, n=5) -> list:
    """Proof that sampled lines are really in what they cite: re-read each line's message or HubSpot record and look
    for the subject, the owner's words or the property value. -> [{'page', 'line', 'source', 'found', 'why'}]."""
    life_home = P._home(life_home)
    mail, hub = mail or Mail(), hub or (Hub() if HUBSPOT_ON else NoHub())
    pool = []
    for slug, e in load_sources(life_home).items():
        pool += [(f'people/{slug}', ev) for ev in e.get('evidence') or []]
    for slug, c in load_companies(life_home).items():
        pool += [(f'companies/{slug}', ev) for ev in c.get('evidence') or []]
    pool.sort(key=lambda x: hashlib.sha1((x[0] + x[1]['id']).encode()).hexdigest())
    mails = [x for x in pool if x[1].get('source') == 'email' and (x[1].get('ref') or {}).get('words')]
    others = [x for x in pool if x[1].get('source') == 'hubspot']
    picks = mails[:max(1, n - 2)] + others[:2]
    out = []
    for page, ev in picks[:n]:
        ref = ev.get('ref') or {}
        try:
            if ev['source'] == 'email':
                m = mail.read(ref['mailbox'], ref['message_id'])
                body = re.sub(r'\s+', ' ', str(m.get('messageText') or ''))
                heads = {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in ((m.get('payload') or {}).get('headers') or [])}
                subj = str(m.get('subject') or heads.get('subject') or '')
                words = str(ref.get('words') or '').removesuffix('...')
                found = (ref.get('subject') or '') == subj and (not words or _clean(words)[:60] in _clean(body))
                why = "subject and the owner's words are in the message" if found else 'not found in the message'
            else:
                rec = hub.get(ref['object'], ref['id'], ref['property'])
                found = str(((rec or {}).get('properties') or {}).get(ref['property'])) == str(ref.get('value'))
                why = f"HubSpot {ref['object'][:-1]} {ref['property']} is the same" if found else 'HubSpot now says something else'
        except Exception as e:
            found, why = False, f'could not re-read: {type(e).__name__}'
        out.append({'page': page, 'line': ev.get('text'), 'source': ev.get('where'), 'found': found, 'why': why})
    return out


# ---------- the real readers (read-only, allow-listed) ----------

COMPOSIO = settings.COMPOSIO_BIN
COMPOSIO_TMP = Path(settings.get('composio.tmp_dir', '/private/tmp/composio'))
GMAIL_READS = frozenset(('GMAIL_FETCH_EMAILS', 'GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID'))
HUBSPOT_READS = re.compile(r'^(?:GET (?:account-info/v3/details|crm/v3/pipelines/deals|crm/v3/objects/(?:contacts|companies|deals)/\d+(?:\?properties=[a-z_,]+)?)'
                           r'|POST (?:crm/v3/objects/(?:contacts|companies|deals)/search|crm/v4/associations/deals/(?:companies|contacts)/batch/read))$')
# Mailbox label -> the Composio Gmail account to read it through (config `mail.composio_accounts`, the same map the
# engine's mail reader uses). A label with no account is not read.
ACCOUNTS = {label: str(acct) for label, acct in (settings.get('mail.composio_accounts', {}) or {}).items() if acct}
# Before a mailbox is read, the connected Gmail account must report exactly the owner's address for that label.
EXPECTED_MAIL = {label: str(addr).casefold() for label, addr in settings.OWNER_EMAILS.items()}
# The mailboxes the pages read: every owner mailbox that has a mail account configured.
BOXES = tuple(b for b in MAILBOX_NAME if b in ACCOUNTS)
# Kept for callers that ask whether HubSpot is paused: with HubSpot not configured, the (zero) portal counts as paused.
PAUSED_PORTALS = set() if HUBSPOT_ON else {HUBSPOT_PORTAL}
# Mail the owner never wants read into a page, whatever inbox it lands in (config `mail.excluded_terms`: words or
# domains; default none). The search excludes them and any message that still mentions one is dropped.
_EXCLUDED = [str(t).strip() for t in settings.get('mail.excluded_terms', []) or [] if str(t).strip()]
STOPPED_MAIL = re.compile('|'.join(r'\b' + re.escape(t).replace(r'\ ', r'[\s_-]+') + r'\b' for t in _EXCLUDED), re.I) if _EXCLUDED else re.compile(r'(?!x)x')


def scoped_search(payload):
    """The excluded terms apply even when such mail arrives in another, allowed mailbox."""
    if not _EXCLUDED:
        return payload
    extra = []
    for t in _EXCLUDED:
        if '.' in t and ' ' not in t:            # a domain: leave out mail from it, to it, or naming it
            extra += [f'-{t}', f'-from:(@{t})', f'-to:(@{t})']
        else:
            extra.append(f'-"{t}"')
    return {**payload, 'query': ((payload.get('query') or '') + ' ' + ' '.join(extra)).strip()}


def allowed_messages(data):
    if isinstance(data, dict) and isinstance(data.get('messages'), list):
        return {**data, 'messages': [m for m in data['messages']
                                     if not STOPPED_MAIL.search(json.dumps(m, ensure_ascii=False))]}
    return data


def _env():
    home = str(Path.home())
    return {'HOME': home, 'PATH': f'{Path(COMPOSIO).parent}:{home}/.local/bin:/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}


def _composio(args: list, stdin: str = '', timeout: int = 120) -> dict:   # the daily pass gives a shorter timeout
    p = subprocess.run([COMPOSIO] + args, input=stdin, capture_output=True, text=True, timeout=timeout, env=_env(), cwd=str(Path.home()))
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f'not JSON (rc {p.returncode})') from None
    if isinstance(data, dict) and data.get('storedInFile') is True:
        path = Path(str(data.get('outputFilePath') or '')).resolve()
        if not path.is_relative_to(COMPOSIO_TMP.resolve()) or not path.is_file():
            raise RuntimeError('unexpected stored output')
        try:
            data = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)         # never leave email text lying in /tmp
    if p.returncode or (isinstance(data, dict) and (data.get('successful') is False or data.get('status') == 'error')):
        raise RuntimeError(f'read failed (rc {p.returncode}): {str((data or {}).get("error") if isinstance(data, dict) else "")[:120]}')
    return data


class Mail:
    """The owner's sent mail and who wrote back, through the same read-only Composio Gmail connection the engine uses."""

    def __init__(self):
        self.calls = 0
        self.verified_accounts = set()

    def account(self, box, timeout=120):
        account = ACCOUNTS.get(box)
        if not account:
            raise PermissionError(f'no mail account is configured for the {box} mailbox (mail.composio_accounts)')
        if box in EXPECTED_MAIL and (box, account) not in self.verified_accounts:
            data = _composio(['proxy', 'https://gmail.googleapis.com/gmail/v1/users/me/profile',
                              '--toolkit', 'gmail', '--account', account], timeout=timeout)
            profile = data.get('data', data)
            if profile.get('emailAddress', '').casefold() != EXPECTED_MAIL[box]:
                raise PermissionError(f'Gmail profile does not match the {box} mailbox')
            self.verified_accounts.add((box, account))
        return account

    def _run(self, slug, payload, box):
        if slug not in GMAIL_READS:
            raise PermissionError(f'{slug} is not a read')
        self.calls += 1
        d = _composio(['execute', slug, '-d', json.dumps(scoped_search(payload) if slug == 'GMAIL_FETCH_EMAILS' else payload), '--account', self.account(box)])
        return allowed_messages(d.get('data', d)) if isinstance(d, dict) else d

    def sent(self, box, days, cap) -> list:
        out, token = [], None
        while len(out) < cap:
            q = {'user_id': 'me', 'query': f'in:sent newer_than:{int(days)}d', 'max_results': min(50, cap - len(out)),
                 'verbose': True, 'include_payload': True}
            if token:
                q['page_token'] = token
            d = self._run('GMAIL_FETCH_EMAILS', q, box)
            msgs = d.get('messages') if isinstance(d, dict) else None
            if not isinstance(msgs, list):
                raise RuntimeError('no message list')     # a failed read is never "nothing was sent"
            for m in msgs:
                heads = {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in ((m.get('payload') or {}).get('headers') or [])}
                out.append({'id': m.get('messageId'), 'thread': m.get('threadId'), 'at': _ts(m.get('messageTimestamp')),
                            'subject': m.get('subject') or heads.get('subject', ''), 'to': heads.get('to') or m.get('to') or '',
                            'cc': heads.get('cc', ''), 'body': m.get('messageText') or '',
                            'automated': bool(heads.get('list-unsubscribe') or heads.get('auto-submitted', 'no').lower() not in ('', 'no')
                                              or heads.get('precedence', '').lower() in ('bulk', 'list'))})
            token = d.get('nextPageToken')
            if not token or not msgs:
                break
        return out[:cap]

    def heard_from(self, box, addresses, days) -> dict:
        """address -> the newest message FROM them in this mailbox in the window (metadata only)."""
        out = {}
        for i in range(0, len(addresses), 20):
            group = {address_key(a): a for a in addresses[i:i + 20]}      # Gmail dots and capitals do not matter
            d = self._run('GMAIL_FETCH_EMAILS', {'user_id': 'me', 'query': f'from:({" OR ".join(group.values())}) newer_than:{int(days)}d -in:sent',
                                                  'max_results': 200, 'verbose': False, 'include_payload': False}, box)
            msgs = d.get('messages') if isinstance(d, dict) else None
            if not isinstance(msgs, list):
                raise RuntimeError('no message list')
            for m in msgs:
                for _, a in _addresses(m.get('sender')):
                    k, at = address_key(a), _ts(m.get('messageTimestamp'))
                    if k in group and (group[k] not in out or (at or 0) > (out[group[k]].get('at') or 0)):
                        out[group[k]] = {'id': m.get('messageId'), 'at': at, 'subject': m.get('subject') or ''}
        return out

    def read(self, box, message_id) -> dict:
        return self._run('GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID', {'user_id': 'me', 'message_id': message_id, 'format': 'full'}, box)


class NoHub:
    """HubSpot is not configured: every read answers "nothing there", and nothing is called."""
    portal_id = HUBSPOT_PORTAL
    calls = 0

    def read(self, addresses, deal_ids=None) -> dict:
        return {'contacts': [], 'companies': [], 'deals': [], 'stages': {}, 'deal_contacts': []}

    def get(self, obj, ident, prop):
        raise PermissionError('HubSpot is not configured (pages.hubspot_account, pages.hubspot_portal)')


class Hub:
    """The configured HubSpot account (pages.hubspot_portal), read only: the portal is checked first, every route is
    allow-listed."""
    portal_id = HUBSPOT_PORTAL
    CONTACT = ['email', 'firstname', 'lastname', 'jobtitle', 'company', 'lifecyclestage', 'lastmodifieddate', 'associatedcompanyid']
    COMPANY = ['name', 'domain', 'industry', 'city', 'state', 'lifecyclestage', 'hs_lastmodifieddate']
    DEAL = ['dealname', 'pipeline', 'dealstage', 'amount', 'closedate', 'hs_lastmodifieddate']

    def __init__(self):
        self.calls = 0

    def _call(self, method, path, body=None):
        if not HUBSPOT_ON or HUBSPOT_PORTAL in PAUSED_PORTALS:
            raise PermissionError('HubSpot is not configured (pages.hubspot_account, pages.hubspot_portal)')
        if not HUBSPOT_READS.match(f'{method} {path}'):
            raise PermissionError(f'{method} {path} is not an allowed read')
        self.calls += 1
        args = ['proxy', 'https://api.hubapi.com/' + path, '--toolkit', 'hubspot', '--account', HUBSPOT_ACCOUNT, '--method', method]
        if body is not None:
            args += ['-H', 'Content-Type: application/json', '-d', '-']
        d = _composio(args, json.dumps(body) if body is not None else '')
        return d.get('data', d) if isinstance(d, dict) and 'data' in d and 'results' not in d else d

    def _search(self, obj, filters, props, cap=200):
        out, after = [], None
        while len(out) < cap:
            body = {'filterGroups': filters, 'properties': props, 'limit': 100, 'sorts': ['hs_object_id']}
            if after:
                body['after'] = after
            page = self._call('POST', f'crm/v3/objects/{obj}/search', body)
            out += page.get('results') or []
            after = ((page.get('paging') or {}).get('next') or {}).get('after')
            if not after:
                break
        return out

    def read(self, addresses, deal_ids=None) -> dict:   # deal_ids = read only these deals (the daily pass); None = every deal (the seed)
        ident = self._call('GET', 'account-info/v3/details')
        if ident.get('portalId') != HUBSPOT_PORTAL:
            raise RuntimeError('the connected HubSpot is not the configured portal (pages.hubspot_portal)')
        stages = {}
        for pipe in (self._call('GET', 'crm/v3/pipelines/deals').get('results') or []):
            stages[str(pipe.get('id'))] = pipe.get('label')
            for st in pipe.get('stages') or []:
                stages[str(st.get('id'))] = st.get('label')
        contacts = []
        for i in range(0, len(addresses), 50):
            contacts += self._search('contacts', [{'filters': [{'propertyName': 'email', 'operator': 'IN', 'values': addresses[i:i + 50]}]}], self.CONTACT)
        domains = sorted({a.split('@')[1] for a in addresses if a.split('@')[1] not in FREE_MAIL})
        companies = []
        for i in range(0, len(domains), 50):
            companies += self._search('companies', [{'filters': [{'propertyName': 'domain', 'operator': 'IN', 'values': domains[i:i + 50]}]}], self.COMPANY)
        deals = self._search('deals', [], self.DEAL, cap=300) if deal_ids is None else [] if not deal_ids else \
            self._search('deals', [{'filters': [{'propertyName': 'hs_object_id', 'operator': 'IN', 'values': sorted(deal_ids)[:100]}]}], self.DEAL)
        ids = [str(d['id']) for d in deals if not why_not_deal(d, stages)]      # skipped and test deals: nothing more is read
        links = {i: {'companies': [], 'contacts': []} for i in ids}
        for to in ('companies', 'contacts'):
            for i in range(0, len(ids), 100):
                got = self._call('POST', f'crm/v4/associations/deals/{to}/batch/read', {'inputs': [{'id': x} for x in ids[i:i + 100]]})
                for r in got.get('results') or []:
                    links.setdefault(str((r.get('from') or {}).get('id')), {'companies': [], 'contacts': []})[to] += \
                        [str(t.get('toObjectId')) for t in r.get('to') or []]
        for d in deals:
            d.update(links.get(str(d['id'])) or {})
        deal_ids = sorted({x for d in deals for x in d.get('contacts') or []} - {str(c['id']) for c in contacts})
        deal_contacts = []
        for i in range(0, len(deal_ids), 50):
            deal_contacts += self._search('contacts', [{'filters': [{'propertyName': 'hs_object_id', 'operator': 'IN', 'values': deal_ids[i:i + 50]}]}], self.CONTACT)
        have = {str(c['id']) for c in companies}
        more = sorted({str((c.get('properties') or {}).get('associatedcompanyid')) for c in contacts + deal_contacts
                       if (c.get('properties') or {}).get('associatedcompanyid')} |
                      {x for d in deals for x in d.get('companies') or []})
        more = [x for x in more if x not in have and x.isdigit()]
        for i in range(0, len(more), 50):
            companies += self._search('companies', [{'filters': [{'propertyName': 'hs_object_id', 'operator': 'IN', 'values': more[i:i + 50]}]}], self.COMPANY)
        return {'contacts': contacts, 'companies': companies, 'deals': deals, 'stages': stages, 'deal_contacts': deal_contacts}

    def get(self, obj, ident, prop):
        return self._call('GET', f'crm/v3/objects/{obj}/{int(ident)}?properties={prop}')


# ---------- hand tool ----------

def main(argv=None):
    """seed --dry | seed --checked [--again] [--days N] | check [N] | show NAME | newest NAME | index   (life folder: LIFE_HOME
    or --life-home). The seed never runs on its own: only this command starts it."""
    a = list(sys.argv[1:] if argv is None else argv)

    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default

    home = opt('--life-home') or os.environ.get('LIFE_HOME')
    if not home:
        print('Give --life-home <dir> or set LIFE_HOME.')
        return 2
    cmd = a[0] if a else ''
    if cmd == 'seed':
        try:
            rep = seed(home, dry='--dry' in a, checked='--checked' in a, again='--again' in a, days=int(opt('--days', 90)),
                       caps={k: int(opt('--' + k.replace('_', '-'))) for k in ('per_mailbox', 'people', 'companies') if opt('--' + k.replace('_', '-'))})
        except Refused as e:
            print(f'Not run: {e}.')
            return 1
        out = opt('--report')
        if out:
            Path(out).write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        rep_short = {k: v for k, v in rep.items() if k != 'samples'}
        print(json.dumps(rep_short, indent=1, ensure_ascii=False, default=str))
        for s in rep['samples']:
            print('\n' + '=' * 70 + '\n' + s['page'] + 'Sources: ' + '; '.join(s['sources']))
        return 0
    if cmd == 'check':
        print(json.dumps(check(home, n=int(a[1]) if len(a) > 1 and a[1].isdigit() else 5), indent=1, ensure_ascii=False))
        return 0
    if cmd == 'index':                              # everyone on file, closest first, from the one record
        print('\n'.join(index_view(home)))
        return 0
    if cmd in ('show', 'newest') and len(a) > 1:
        name = ' '.join(x for x in a[1:] if not x.startswith('--') and x != home)
        if cmd == 'newest':
            print(json.dumps(newest_for(home, name), indent=1, ensure_ascii=False))
            return 0
        for kind, slug, _ in named(home, name)[:1]:
            print((P._dir(home) if kind == 'person' else Path(home) / 'companies').joinpath(f'{slug}.md').read_text())
            return 0
        print('Nobody (or more than one page) is known by that name.')
        return 0
    print(main.__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
