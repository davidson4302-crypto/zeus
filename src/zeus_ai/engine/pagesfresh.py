"""pagesfresh: the person and company pages take in what is new, once a day, then hourly for inboxes with new mail.

Why: pages.py fills the pages ONCE, by hand (the seed). Without this pass nothing adds to them afterwards, so "where
are we with Acme?" gets a day staler every day and the owner ends up checking the inbox themself.

What this does, once a day (the first engine run at or after 6:00am on the owner's clock; a read that failed is tried
again two hours later; a run that ran out of time picks up on the next engine run where it stopped):
  mail      in each inbox the seed reads: the mail the owner SENT since the last good read of that inbox, and the
            mail the people who have a page (and the people the owner wrote to since the seed) sent back since then
  HubSpot   only when configured (pages.hubspot_account, pages.hubspot_portal): contacts and companies that have a
            page and changed since the last good read, and every deal that changed
and folds it into people/sources.json and companies/index.json with the seed's own wording, filters and add-only
rule (pages.py: why_not_message, robot, why_not_deal, _his_words, _subject, _deal_line, _add). Then only the pages
that got a new line are written again, by the page code itself (pages.render), so the short summary on top is
rewritten the way it always is. A page with nothing new is not opened, not rendered and not written.
After the day's first pass, at most once an hour, the inboxes that got new mail since the last pass are read again
(touched(), from the ledger's own record of mail seen, so finding that out reads nothing), and the newest received
message behind each new line is opened so the line carries the sender's first words, not only the subject
(their_words()). No model is called here, in any hour.

Rules it keeps:
- Never a re-seed. `pages.seed` is not called. Someone new gets a page only by the seed's own bar, decided by the
  seed's own code (pages.plan): the owner wrote to them and they wrote back, or they are on a real HubSpot deal. And
  never wider than what the seed took in: a deal that is not on a page yet counts only when it CHANGED since the last
  good read (never every deal in the portal) and sits in a sales pipeline the pages already hold a deal from, so a
  large pipeline nobody filed never sweeps its contacts into pages.
- Every new line has an id, a date and where it came from. A line with an id already on the page is never added
  again. Several emails in one thread, the same way, on one day are ONE line that says how many.
- Nothing is edited or removed. When HubSpot now says something else (a new title, a new company, a deal that moved
  or changed its amount) the new line is added beside the old one, and the page code's summary says the newest and,
  where two sources disagree, both.
- "Could not read" is never "nothing new". A source that failed keeps its place (the next good read starts where
  the last good one ended), and the state file says in plain words what is missing: "Mail in Alex's work inbox
  could not be read on Nov 15, so this is partial." The chat is handed that sentence with the page (pages.chat_units)
  and health.json carries it.
- Reads only, through the seed's allow-listed read routes (pages.GMAIL_READS, pages.HUBSPOT_READS). There is no
  send, no write to mail or HubSpot, no model call, no ledger write, no item for the note. Never sends.
- Mail and HubSpot text is untrusted data. It is only ever quoted (a subject, the owner's own first words, a field
  value) after pages._clean; nothing in it is followed, and no model reads it here. A subject the engine's own code
  check (screen.py) flags as written to steer an assistant is not quoted and not kept: the line says it was left out.
- Bounded: a time budget per engine run, a timeout per read, caps on how much is read, and a list of the steps
  still to do (the cursor) kept in the state file:
  * slow reads: each step reads one fixed window until it is read through, and the answers it already got are kept
    (pages-fresh-reads.json), so a step cut off by the time limit goes on from its first unread answer on the next
    engine run instead of starting over; the portal check alone is asked again each time. While a step is behind,
    the state says so ("HubSpot is taking more than one run to read ..., so this is partial until it is caught up").
  * more mail than one run reads: the cursor moves only past what was read in full. Gmail gives the newest first,
    so the older part is read next (`upto`), then the cursor jumps to where that window ended (`high`). Whole days
    are kept where possible, so one thread's emails of one day stay one line.
  * the seed's bar, exactly: an address with no display name is not dropped; pages.plan names it from HubSpot or
    skips it ("no name to file them under"), as the seed does. Addresses over the per-run check cap wait, unchecked,
    for the next run, and that is said. Any mail FROM someone counts as writing back, auto-replies included, because
    the seed counts it so (pages.Mail.heard_from reads metadata only).

Files in the life folder:
  pages-fresh-on          the switch. Without it nothing here runs and nothing is written.
  pages-fresh.json        the state: where each source was last read up to, today's run and its steps still to do,
                          what could not be read and since when, the plain "partial" sentences, the people the owner
                          wrote to who have not written back yet
  pages-fresh.log.jsonl   one line per run slice: counts and the ids of the lines added (no mail text)
  pages-fresh-reads.json  only while a step is part-read: the answers it already got (can hold mail text); removed
                          as soon as no step needs it

By hand: python3 -I -B pagesfresh.py run|status --life-home <dir> [--force]   (run takes the tick's lock)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pages as G  # noqa: E402
import people as P  # noqa: E402
import settings  # noqa: E402

OWNER = G.OWNER

SWITCH = 'pages-fresh-on'
FILE = 'pages-fresh.json'
LOG = 'pages-fresh.log.jsonl'
START_HOUR = 6                   # the first engine run at or after 6:00am on the owner's clock
RETRY_AFTER = 2 * 3600           # a failed run tries again after 2 hours
BUDGET_S = 60.0                  # no new step is started after this many seconds of one engine run
HARD_S = 110.0                   # no new read is started after this many (a step in flight is then cut, not failed)
CALL_TIMEOUT = 40                # seconds for one read
OVERLAP = 15 * 60                # each read starts this long before the last good one ended (clocks, slow indexes); ids keep it from doubling
LOOK_BACK_CAP = 30 * 86400       # the switch was off for longer than this: only this far back is read, and the state says so
SENT_CAP = 100                   # sent messages read per inbox per run
HEARD_CAP = 100                  # received messages listed per group of addresses per run
GROUP = 25                       # addresses per Gmail search
NEW_CAP = 40                     # new addresses checked against the seed's bar per inbox per run
NEW_PAGES = 24                   # addresses put to the seed's bar in one run; more than that wait for the next run and that is said
DEALS_PER_RUN = 8                # changed deals that name someone new, put to the seed's bar in one run; the rest wait and that is said
PENDING_MAX = 200                # people the owner wrote to who have not written back, kept at most (more than that is said)
PENDING_DAYS = 30
DECLINED_DAYS = 14               # an address the seed's bar turned down is not put to it again for this long
BAR_DAYS = 90                    # the seed's window: "wrote back in the window"
STALE_AFTER = G.FRESH_STALE_AFTER
BOXES = G.BOXES                  # the same inboxes the seed reads
READS = 'pages-fresh-reads.json'  # the answers a step cut off by the time limit already got (its cursor inside the reads)
PASS_EVERY = 3600                # after the day's first pass, one more pass an hour at most, only for inboxes with new mail
BODY_READS = 12                  # received messages opened per inbox per pass (the newest behind each new line)
BODY_SHOWN = 9000                # as much of a received body as is looked at (sources.BODY_SHOWN, research's own cut)
THEIR_WORDS = 160                # as much of it as is quoted on a page (pages._his_words' cap for the owner's own first words)
MEMO_ROOM = 4_000_000            # bytes of such answers kept at most; past that a cut step simply reads again


class OutOfTime(Exception):
    """The run's hard time limit passed before a read could start. The step is cut and done again on the next run."""


# ---------- small helpers ----------

def on(life) -> bool:
    try:
        return (Path(life) / SWITCH).exists()
    except Exception:
        return False


def _day_of(ts) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(float(ts)))


def load_state(life) -> dict:
    try:
        d = json.loads((Path(life) / FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_state(life, st: dict):
    P._write(Path(life) / FILE, json.dumps(st, indent=2, ensure_ascii=False, sort_keys=True))


def _seed_at(life):
    try:
        return float(json.loads((Path(life) / 'pages-seeded.json').read_text(encoding='utf-8'))['at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _first_since(seeded: float) -> float:
    """Where the very first daily read starts: the start of the day the seed ran, not the minute it ran. The seed kept
    only the NEWEST mail each person sent, so their earlier mail of that same day is on no page yet; ids keep
    what the seed did file from being added twice."""
    t = time.localtime(float(seeded))
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def source_name(src: str) -> str:
    """'mail:work' -> "Mail in Alex's work inbox"; 'hubspot' -> 'HubSpot'; 'new' -> the new-people step."""
    if src.startswith('mail:'):
        return f"Mail in {OWNER}'s {G.MAILBOX_NAME.get(src[5:], src[5:])} inbox"
    return 'HubSpot' if src == 'hubspot' else 'The check for people new to the pages'


def sentences(st: dict) -> list:
    """The plain "partial" lines for the state, the chat and health.json. [] when every source was read."""
    bad = st.get('could_not_read') or {}
    out = []
    boxes = sorted(s for s in bad if s.startswith('mail:'))
    if boxes and len(boxes) == len(BOXES):
        first = min(bad[s].get('since') or 0 for s in boxes)
        last = max(bad[s].get('last') or 0 for s in boxes)
        out.append(f"{OWNER}'s mail " + _when(first, last) + ', so this is partial.')
        boxes = []
    for s in boxes + sorted(s for s in bad if not s.startswith('mail:')):
        if s == 'new':
            out.append(f"People new since {P._day(bad[s].get('since'))} could not be checked for a page yet, so this is partial.")
        else:
            out.append(f"{source_name(s)} {_when(bad[s].get('since'), bad[s].get('last'))}, so this is partial.")
    for s, b in sorted((st.get('behind') or {}).items()):
        if s in bad:
            continue
        if b.get('why') == 'more':
            out.append(f"{source_name(s)} had more new since {P._day(b.get('since'))} than one run reads, so this is partial until it is caught up.")
        else:
            out.append(f"{source_name(s)} is taking more than one run to read (since {P._day(b.get('since'))}), so this is partial until it is caught up.")
    for s, note in sorted((st.get('capped') or {}).items()):
        if s == 'new':
            out.append(f"More people were new on {P._day(note.get('at'))} than one run adds to the pages, so this is partial.")
        else:
            out.append(f"{source_name(s)} had more new on {P._day(note.get('at'))} than one run reads, so this is partial.")
    return out


def _when(first, last) -> str:
    if _day_of(first or 0) == _day_of(last or first or 0):
        return f"could not be read on {P._day(last or first)}"
    return f"has not been readable since {P._day(first)}"


def _ids(entry: dict) -> set:
    """Every message or record already on a page: the line ids and the messages folded into a combined line."""
    out = set()
    for e in entry.get('evidence') or []:
        out.add(e.get('id'))
        ref = e.get('ref') or {}
        for x in ref.get('also') or []:
            out.add(f"mail:{ref.get('mailbox')}:{x}")
    return out


def _slot(e: dict) -> tuple:
    ref = e.get('ref') or {}
    return ((e.get('claim') or {}).get('field'), ref.get('object'), str(ref.get('id')))


def _slot_add(entry: dict, ev: dict) -> bool:
    """A HubSpot line. The same thing HubSpot already said last (same record, same field, same words) is a repeat and
    is not added. Anything else is added beside the older lines, never over them."""
    same = [e for e in entry.get('evidence') or [] if e.get('source') == 'hubspot' and _slot(e) == _slot(ev)]
    if same and max(same, key=lambda e: e.get('at') or 0).get('text') == ev['text']:
        return False
    have = {e.get('id') for e in entry.get('evidence') or []}
    if ev['id'] in have:
        ev['id'] = f"{ev['id']}:{G._iso(ev.get('at'))}"
        if ev['id'] in have:
            ev['id'] += ':' + hashlib.sha1(ev['text'].encode()).hexdigest()[:6]
            if ev['id'] in have:
                return False
    return G._add(entry, ev)


def _theirs(e: dict) -> bool:
    return str(e.get('where') or '').startswith('an email from ')


def _company_has(entry: dict, box, subject, at, theirs: bool) -> bool:
    """A company page already has a line for this thread, this way, on this day (the seed kept one line per thread)."""
    for e in entry.get('evidence') or []:
        ref = e.get('ref') or {}
        if e.get('source') == 'email' and ref.get('mailbox') == box and _theirs(e) == theirs and e.get('at') \
                and _day_of(e['at']) == _day_of(at) and G._bare(ref.get('subject')) == G._bare(subject):
            return True
    return False


def _maps(src_all: dict, comp_all: dict) -> dict:
    by_addr = {G.address_key(a): slug for slug, x in src_all.items() for a in x.get('emails') or []}
    comps_of = {}
    for slug, x in src_all.items():
        got = [x['company']] if x.get('company') in comp_all else []
        got += [c for c, y in comp_all.items() if slug in (y.get('people') or []) and c not in got]
        comps_of[slug] = got
    return {'by_addr': by_addr, 'comps_of': comps_of,
            'by_hub': {str(c.get('hubspot_id')): slug for slug, c in comp_all.items() if c.get('hubspot_id')},
            'by_domain': {d: slug for slug, c in comp_all.items() for d in c.get('domains') or []}}


def _sent_shape(m: dict) -> dict:
    """One Gmail message as the seed shaped it (pages.Mail.sent)."""
    heads = {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in ((m.get('payload') or {}).get('headers') or [])}
    return {'id': m.get('messageId'), 'thread': m.get('threadId'), 'at': G._ts(m.get('messageTimestamp')),
            'subject': m.get('subject') or heads.get('subject', ''), 'to': heads.get('to') or m.get('to') or '',
            'cc': heads.get('cc', ''), 'body': m.get('messageText') or '',
            'automated': bool(heads.get('list-unsubscribe') or heads.get('auto-submitted', 'no').lower() not in ('', 'no')
                              or heads.get('precedence', '').lower() in ('bulk', 'list'))}


def _load_memo(life) -> dict:
    try:
        d = json.loads((Path(life) / READS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _save_memo(life, memo: dict):
    """Kept only while a step is part-read; removed as soon as no step needs it (it can hold mail text)."""
    keep = {k: v for k, v in memo.items() if v}
    f = Path(life) / READS
    if keep:
        P._write(f, json.dumps(keep, ensure_ascii=False, sort_keys=True))
    else:
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def _next_midnight(at: float) -> float:
    t = time.localtime(float(at))
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def _read_down_to(sent: list, more, heard: list, more2, frm: float, top: float):
    """An inbox had more new mail than one run reads. Gmail gives the newest first, so everything from some time up
    to `top` was read in full and the older part was not. -> the time from which this slice's mail is kept (the rest
    is read on the next slice), or None when it cannot be told (the old way: what did not fit is said, not read).
    Whole days are kept where possible, so one thread's emails of one day stay one line."""
    bounds = []
    if more:
        if not sent:
            return None
        bounds.append(min(float(m.get('at') or 0) for m in sent))
    if more2:
        if isinstance(more2, bool):
            if not heard:
                return None
            bounds.append(min(float(h.get('at') or 0) for h in heard))
        else:
            bounds.append(float(more2))
    b = max(bounds)
    if b <= frm:
        return None
    for keep in (_next_midnight(b), int(b) + 1):         # whole days if a whole day was read, else the whole seconds that were
        if keep < top and any(float(x.get('at') or 0) >= keep for x in sent + heard):
            return float(keep)
    return None                                        # more than one read's worth inside one second: cannot page down


# ---------- the readers (the seed's, with a time limit and a "since") ----------

def _remembered(reader, key: str, read):
    """The step's cursor inside a read: an answer this step already got (same request, same window) is used again
    instead of being read again, so a step cut off by the time limit goes on from the first read it has not had yet.
    reader.memo: the step's saved answers (run() loads and saves them), or None = remember nothing."""
    memo = getattr(reader, 'memo', None)
    if memo is not None and key in memo:
        return json.loads(json.dumps(memo[key]))
    got = read()
    if memo is not None and getattr(reader, 'room', 0) > 0:
        size = len(json.dumps(got, ensure_ascii=False))
        if size <= reader.room:
            memo[key] = got
            reader.room -= size
    return json.loads(json.dumps(got)) if memo is not None else got


class Mail(G.Mail):
    """pages.Mail plus "since a time". The same two allow-listed Gmail read actions and nothing else."""

    def __init__(self, deadline=None, clock=time.monotonic):
        super().__init__()
        self.deadline, self.clock, self.memo, self.room = deadline, clock, None, 0

    def _run(self, slug, payload, box):
        if slug not in G.GMAIL_READS:
            raise PermissionError(f'{slug} is not a read')

        if slug == 'GMAIL_FETCH_EMAILS':
            payload = G.scoped_search(payload)

        def read():
            if self.deadline is not None and self.clock() >= self.deadline:
                raise OutOfTime()
            self.calls += 1
            account = self.account(box, CALL_TIMEOUT)
            d = G._composio(['execute', slug, '-d', json.dumps(payload), '--account', account], timeout=CALL_TIMEOUT)
            return d.get('data', d) if isinstance(d, dict) else d
        return G.allowed_messages(_remembered(self, json.dumps(['gmail', slug, payload, box], sort_keys=True), read))

    @staticmethod
    def _window(since, until) -> str:
        return f' after:{int(since)}' + (f' before:{int(until)}' if until else '')

    def sent_since(self, box, since, until=None, cap=SENT_CAP, to=None):
        """-> ([message as the seed shaped it], more were there than read). `to`: only mail to these addresses."""
        out, token, more = [], None, False
        who = f' to:({" OR ".join(to)})' if to else ''
        while len(out) < cap:
            q = {'user_id': 'me', 'query': 'in:sent' + who + self._window(since, until), 'max_results': min(50, cap - len(out)),
                 'verbose': True, 'include_payload': True}
            if token:
                q['page_token'] = token
            d = self._run('GMAIL_FETCH_EMAILS', q, box)
            msgs = d.get('messages') if isinstance(d, dict) else None
            if not isinstance(msgs, list):
                raise RuntimeError('no message list')         # a failed read is never "nothing was sent"
            out += [_sent_shape(m) for m in msgs]
            token = d.get('nextPageToken')
            if not token or not msgs:
                break
        return out[:cap], bool(token) or len(out) > cap

    def received_since(self, box, addresses, since, until=None, cap=HEARD_CAP):
        """Every message FROM these addresses in this inbox since then (subject and time only, never the text).
        -> ([{'id', 'thread', 'at', 'subject', 'from'}], more): more is False when everything was read, else the time
        down to which every group was read in full (Gmail gives the newest first, so what is missing is older)."""
        out, more = [], False
        for i in range(0, len(addresses), GROUP):
            group = {G.address_key(a): a for a in addresses[i:i + GROUP]}
            d = self._run('GMAIL_FETCH_EMAILS', {'user_id': 'me', 'query': f'from:({" OR ".join(group.values())}) -in:sent' + self._window(since, until),
                                                  'max_results': cap, 'verbose': False, 'include_payload': False}, box)
            msgs = d.get('messages') if isinstance(d, dict) else None
            if not isinstance(msgs, list):
                raise RuntimeError('no message list')
            if d.get('nextPageToken'):
                oldest = min([G._ts(m.get('messageTimestamp')) or 0 for m in msgs] or [0])
                more = max(float(more or 0), oldest) or True       # True: more there, but no time to page down to
            for m in msgs:
                for _, a in G._addresses(m.get('sender')):
                    if G.address_key(a) in group:
                        out.append({'id': m.get('messageId'), 'thread': m.get('threadId'), 'at': G._ts(m.get('messageTimestamp')),
                                    'subject': m.get('subject') or '', 'from': G.address_key(a)})
        return out, more


    def their_words(self, box, message_id) -> tuple:
        """The first words of one received message -> (words, left_out). Received mail is listed with
        `include_payload=False`, so without this a page says "Taylor wrote to Alex about <subject>" and nothing of what
        was written. One read by message id (already an allow-listed read). The body is somebody else's text: cut at
        research's size, quoted history and signature dropped, cleaned the way a subject is, kept to one short quote,
        and never followed. When the engine's code check (screen.py) says it is written to steer an assistant, no
        word of it is kept: ('', True)."""
        d = self._run('GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID', {'user_id': 'me', 'message_id': message_id, 'format': 'full'}, box)
        if not isinstance(d, dict) or G.STOPPED_MAIL.search(json.dumps(d, ensure_ascii=False)):
            return '', False
        body = str(d.get('messageText') or '')[:BODY_SHOWN]
        try:
            if __import__('screen').scan(str(d.get('subject') or ''), body).get('flag'):
                return '', True
        except Exception:
            return '', False                               # the check itself broke: nothing of the body is quoted
        return G._his_words(body, THEIR_WORDS), False


def _with_words(mail, box: str, heard: list, src_all: dict) -> list:
    """Open the newest received message behind each line that is about to be added (one per person, thread and day;
    BODY_READS at most per inbox per pass). Everything else keeps a subject-only line. A body that
    cannot be read leaves that one line subject-only; running out of time cuts the step like any other read."""
    read = getattr(mail, 'their_words', None)
    if read is None:
        return heard                                       # a stand-in reader without it: subjects only
    by_addr = {G.address_key(a): slug for slug, x in src_all.items() for a in x.get('emails') or []}
    by_person, n = {}, 0
    for h in heard:
        slug = by_addr.get(h.get('from'))
        if slug and h.get('id') and f"mail:{box}:{h['id']}" not in _ids(src_all[slug]):
            by_person.setdefault(slug, []).append(h)
    for msgs in by_person.values():
        for grp in _groups(msgs, lambda m: False):
            if n >= BODY_READS:
                return heard
            n += 1
            try:
                grp[0]['words'], grp[0]['words_left_out'] = read(box, grp[0]['id'])
            except OutOfTime:
                raise
            except Exception:
                pass
    return heard


class Hub(G.Hub):
    """pages.Hub plus "what changed since". The same allow-listed read routes, the portal checked first."""

    def __init__(self, deadline=None, clock=time.monotonic):
        super().__init__()
        self.deadline, self.clock, self.memo, self.room = deadline, clock, None, 0

    def _call(self, method, path, body=None):
        if self.deadline is not None and self.clock() >= self.deadline:
            raise OutOfTime()
        if not G.HUBSPOT_ON or G.HUBSPOT_PORTAL in G.PAUSED_PORTALS:
            raise PermissionError('HubSpot is not configured (pages.hubspot_account, pages.hubspot_portal)')
        if not G.HUBSPOT_READS.match(f'{method} {path}'):
            raise PermissionError(f'{method} {path} is not an allowed read')

        def read():
            if self.deadline is not None and self.clock() >= self.deadline:
                raise OutOfTime()
            self.calls += 1
            args = ['proxy', 'https://api.hubapi.com/' + path, '--toolkit', 'hubspot', '--account', G.HUBSPOT_ACCOUNT, '--method', method]
            if body is not None:
                args += ['-H', 'Content-Type: application/json', '-d', '-']
            d = G._composio(args, json.dumps(body) if body is not None else '', timeout=CALL_TIMEOUT)
            return d.get('data', d) if isinstance(d, dict) and 'data' in d and 'results' not in d else d
        if path.startswith('account-info/'):
            return read()                                  # which portal this is: asked again on every slice, never remembered
        return _remembered(self, json.dumps(['hubspot', method, path, body], sort_keys=True), read)

    def changed(self, since, until, emails, company_ids) -> dict:
        """Contacts (of these addresses) and companies (of these ids) changed in the window, and every deal changed in
        it, with the deals' links and the contacts on them. -> the shape pages.Hub.read gives."""
        ident = self._call('GET', 'account-info/v3/details')
        if ident.get('portalId') != G.HUBSPOT_PORTAL:
            raise RuntimeError('the connected HubSpot is not the configured portal (pages.hubspot_portal)')

        def window(prop):
            f = [{'propertyName': prop, 'operator': 'GTE', 'value': str(int(since * 1000))}]
            return f + ([{'propertyName': prop, 'operator': 'LTE', 'value': str(int(until * 1000))}] if until else [])

        contacts, companies = [], []
        emails = sorted(emails)
        for i in range(0, len(emails), 50):
            contacts += self._search('contacts', [{'filters': [{'propertyName': 'email', 'operator': 'IN', 'values': emails[i:i + 50]}]
                                                   + window('lastmodifieddate')}], self.CONTACT)
        ids = sorted(x for x in company_ids if str(x).isdigit())
        for i in range(0, len(ids), 50):
            companies += self._search('companies', [{'filters': [{'propertyName': 'hs_object_id', 'operator': 'IN', 'values': ids[i:i + 50]}]
                                                     + window('hs_lastmodifieddate')}], self.COMPANY)
        deals = self._search('deals', [{'filters': window('hs_lastmodifieddate')}], self.DEAL, cap=200)
        stages, deal_contacts = {}, []
        if deals:
            for pipe in (self._call('GET', 'crm/v3/pipelines/deals').get('results') or []):
                stages[str(pipe.get('id'))] = pipe.get('label')
                for st in pipe.get('stages') or []:
                    stages[str(st.get('id'))] = st.get('label')
            real = [str(d['id']) for d in deals if not G.why_not_deal(d, stages)]     # skipped and test deals: nothing more is read
            links = {i: {'companies': [], 'contacts': []} for i in real}
            for to in ('companies', 'contacts'):
                for i in range(0, len(real), 100):
                    got = self._call('POST', f'crm/v4/associations/deals/{to}/batch/read', {'inputs': [{'id': x} for x in real[i:i + 100]]})
                    for r in got.get('results') or []:
                        links.setdefault(str((r.get('from') or {}).get('id')), {'companies': [], 'contacts': []})[to] += \
                            [str(t.get('toObjectId')) for t in r.get('to') or []]
            for d in deals:
                d.update(links.get(str(d['id'])) or {})
            want = sorted({x for d in deals for x in d.get('contacts') or []})
            for i in range(0, len(want), 50):
                deal_contacts += self._search('contacts', [{'filters': [{'propertyName': 'hs_object_id', 'operator': 'IN', 'values': want[i:i + 50]}]}], self.CONTACT)
        return {'contacts': contacts, 'companies': companies, 'deals': deals, 'stages': stages, 'deal_contacts': deal_contacts}

    def pipelines_of(self, deal_ids) -> dict:
        """deal id -> the id of the pipeline it is in, for the deals already on a page."""
        out = {}
        ids = sorted(str(x) for x in deal_ids if str(x).isdigit())
        for i in range(0, len(ids), 100):
            for d in self._search('deals', [{'filters': [{'propertyName': 'hs_object_id', 'operator': 'IN', 'values': ids[i:i + 100]}]}], ['pipeline']):
                out[str(d.get('id'))] = str((d.get('properties') or {}).get('pipeline'))
        return out


# ---------- folding mail in ----------

def _groups(msgs: list, key) -> list:
    """Messages -> [[newest, older ...]] : one group per thread, per day, per `key`."""
    got = {}
    for m in sorted(msgs, key=lambda m: -(m.get('at') or 0)):
        got.setdefault((key(m), m.get('thread') or m.get('id'), _day_of(m.get('at') or 0)), []).append(m)
    return list(got.values())


def _count(n: int) -> str:
    return f' ({n} emails that day)' if n > 1 else ''


LEFT_OUT = ' (subject left out: it reads like instructions to an assistant)'


def _about(subject) -> tuple:
    """How a line names an email's subject -> (' about "<subject>"', the subject kept in the line's ref). A subject is
    somebody else's text. It is only ever quoted, cleaned the way the seed cleans it. When the engine's own code check
    (screen.py) says it is written to steer an assistant, it is not quoted at all and not kept: (LEFT_OUT, '')."""
    try:
        if __import__('screen').scan(str(subject or ''), '').get('flag'):
            return LEFT_OUT, ''
    except Exception:
        pass
    return f' about "{G._subject(subject)}"', str(subject or '')


def fold_mail(box: str, sent: list, heard: list, src_all: dict, comp_all: dict, now: float, added: list, stats: dict) -> dict:
    """Pure: one inbox's new sent and received mail onto the pages of the people (and their companies) who have one.
    -> {'people': {slugs touched}, 'companies': {...}, 'new': {address key: {'address', 'name', 'box', 'at'}}}.
    `new` = people the owner wrote to who have no page: the seed's bar decides about them later."""
    maps = _maps(src_all, comp_all)
    touched = {'people': set(), 'companies': set(), 'new': {}}

    def skip(why):
        stats[why] = stats.get(why, 0) + 1

    def note(page, ev):
        added.append({'page': page, 'id': ev['id'], 'at': ev.get('at'), 'where': ev.get('where'), 'text': ev.get('text')})

    per_person, per_company = {}, {}
    for m in sent:
        why = G.why_not_message(m)
        if why:
            skip(why)
            continue
        people = [(n, a) for n, a in G._addresses(m.get('to'), m.get('cc')) if G.address_key(a) not in G.OWN_KEYS]
        to = {G.address_key(a) for _, a in G._addresses(m.get('to'))}
        for n, a in people:
            why = G.robot(a, n)
            if why:
                skip(why)
                continue
            k = G.address_key(a)
            slug = maps['by_addr'].get(k)
            if not slug:
                c = touched['new'].setdefault(k, {'address': a.lower(), 'name': '', 'box': box, 'at': 0})
                c['name'] = n or c['name']
                c['at'] = max(c['at'], m.get('at') or 0)
                continue
            shaped = {**m, 'mailbox': box, 'others': len(people) - 1, 'copied': k not in to}
            if f"mail:{box}:{m['id']}" not in _ids(src_all[slug]):
                per_person.setdefault(slug, []).append(shaped)
            for cs in maps['comps_of'].get(slug) or []:
                per_company.setdefault(cs, []).append({**shaped, 'who': src_all[slug].get('name') or slug})
    for slug, msgs in per_person.items():
        entry = src_all[slug]
        first = G._first(entry.get('name') or slug)
        for grp in _groups(msgs, lambda m: m['copied']):
            m = grp[0]
            words = '' if m['copied'] else G._his_words(m.get('body'))      # copied: the words were for someone else
            others = f' and {m["others"]} other{"s" if m["others"] > 1 else ""}' if m.get('others') else ''
            about, kept = _about(m.get('subject'))
            if m['copied']:
                text = f'{OWNER} copied {first} on an email{about}{_count(len(grp))}.'
            else:
                text = f'{OWNER} emailed {first}{others}{about}{_count(len(grp))}' + (f': "{words}"' if words else '')
                text += '' if text.endswith(('."', '!"', '?"', '..."')) else '.'
            ref = {'mailbox': box, 'message_id': m['id'], 'thread_id': m.get('thread'), 'subject': kept, 'words': words}
            if len(grp) > 1:
                ref['also'] = [x['id'] for x in grp[1:]]
            ev = {'id': f"mail:{box}:{m['id']}", 'at': m.get('at'), 'source': 'email',
                  'where': G.sent_from(box), 'text': text, 'ref': ref, 'read_at': now}
            if G._add(entry, ev):
                touched['people'].add(slug)
                note(f'people/{slug}', ev)
    for cs, msgs in per_company.items():
        entry = comp_all[cs]
        for grp in _groups(msgs, lambda m: False):
            m = grp[0]
            if f"mail:{box}:{m['id']}" in _ids(entry) or _company_has(entry, box, m.get('subject'), m.get('at') or 0, False):
                continue
            about, kept = _about(m.get('subject'))
            ev = {'id': f"mail:{box}:{m['id']}", 'at': m.get('at'), 'source': 'email', 'where': G.sent_from(box),
                  'text': f'{OWNER} emailed {m["who"]} there{about}.',
                  'ref': {'mailbox': box, 'message_id': m['id'], 'thread_id': m.get('thread'), 'subject': kept}, 'read_at': now}
            if G._add(entry, ev):
                touched['companies'].add(cs)
                note(f'companies/{cs}', ev)
    by_person = {}
    for h in heard:
        slug = maps['by_addr'].get(h.get('from'))
        if slug and h.get('id'):
            by_person.setdefault(slug, []).append(h)
    for slug, msgs in by_person.items():
        entry = src_all[slug]
        first = G._first(entry.get('name') or slug)
        where = G.sent_to(first, box)
        fresh = [h for h in msgs if f"mail:{box}:{h['id']}" not in _ids(entry)]
        for grp in _groups(fresh, lambda m: False):
            h = grp[0]
            about, kept = _about(h.get('subject'))
            ref = {'mailbox': box, 'message_id': h['id'], 'thread_id': h.get('thread'), 'subject': kept}
            if len(grp) > 1:
                ref['also'] = [x['id'] for x in grp[1:]]
            text = f'{first} wrote to {OWNER}{about}{_count(len(grp))}'
            if h.get('words'):                             # what they wrote, not only the subject (quoted, never followed)
                text += f': "{h["words"]}"'
                ref['words'] = h['words']
            elif h.get('words_left_out'):
                text += ' (their words left out: they read like instructions to an assistant)'
            text += '' if text.endswith(('."', '!"', '?"', '..."')) else '.'
            ev = {'id': f"mail:{box}:{h['id']}", 'at': h.get('at'), 'source': 'email', 'where': where,
                  'text': text, 'ref': ref, 'read_at': now}
            if G._add(entry, ev):
                touched['people'].add(slug)
                note(f'people/{slug}', ev)
        for cs in maps['comps_of'].get(slug) or []:
            centry = comp_all[cs]
            for grp in _groups(msgs, lambda m: False):
                h = grp[0]
                if f"mail:{box}:{h['id']}" in _ids(centry) or _company_has(centry, box, h.get('subject'), h.get('at') or 0, True):
                    continue
                about, kept = _about(h.get('subject'))
                ev = {'id': f"mail:{box}:{h['id']}", 'at': h.get('at'), 'source': 'email', 'where': where,
                      'text': f'{entry.get("name") or first} wrote to {OWNER}{about}.',
                      'ref': {'mailbox': box, 'message_id': h['id'], 'thread_id': h.get('thread'), 'subject': kept}, 'read_at': now}
                if G._add(centry, ev):
                    touched['companies'].add(cs)
                    note(f'companies/{cs}', ev)
    return touched


# ---------- folding HubSpot in ----------

def deals_on_pages(src_all: dict, comp_all: dict) -> set:
    return {str((e.get('ref') or {}).get('id')) for store in (src_all, comp_all) for x in store.values()
            for e in x.get('evidence') or [] if (e.get('ref') or {}).get('object') == 'deals'}


def fold_hub(hub: dict, src_all: dict, comp_all: dict, now: float, added: list, stats: dict, allowed=()) -> dict:
    """Pure: HubSpot records that changed onto the pages that already exist, in the seed's own wording.
    allowed: the pipelines the pages already hold a deal from. A changed deal that is on no page yet is taken only
    from one of those; a deal already on a page is always followed.
    -> {'people', 'companies', 'new_deals': ids of changed real deals that name someone or a company with no page (the
    seed's bar decides about those, and only those: never every deal in the portal)}."""
    maps = _maps(src_all, comp_all)
    touched = {'people': set(), 'companies': set(), 'new_deals': []}
    stages = hub.get('stages') or {}

    def note(page, ev):
        added.append({'page': page, 'id': ev['id'], 'at': ev.get('at'), 'where': ev.get('where'), 'text': ev.get('text')})

    for hc in hub.get('contacts') or []:
        hp = hc.get('properties') or {}
        slug = maps['by_addr'].get(G.address_key(hp.get('email')))
        if not slug:
            continue
        entry = src_all[slug]
        title, cname = G._clean(hp.get('jobtitle'), 60), G._clean(hp.get('company'), 60)
        at = G._ts(hp.get('lastmodifieddate')) or now
        lines = []
        if title:
            who = title + (f' at {cname}' if cname else '')
            lines.append({'id': f"hubspot:contact:{hc.get('id')}:who:{G._norm(who)}", 'at': at, 'source': 'hubspot', 'where': "the HubSpot contact",
                          'text': f'HubSpot has {entry["name"]} as {who}.', 'claim': {'field': 'who', 'value': who},
                          'ref': {'object': 'contacts', 'id': str(hc.get('id')), 'property': 'jobtitle', 'value': hp.get('jobtitle')}, 'read_at': now})
        if cname:
            lines.append({'id': f"hubspot:contact:{hc.get('id')}:company:{G._norm(cname)}", 'at': at, 'source': 'hubspot',
                          'where': "the HubSpot contact record", 'text': f'HubSpot lists {entry["name"]} at {cname}.',
                          'claim': {'field': 'company', 'value': cname},
                          'ref': {'object': 'contacts', 'id': str(hc.get('id')), 'property': 'company', 'value': hp.get('company')}, 'read_at': now})
        for ev in lines:
            if _slot_add(entry, ev):
                touched['people'].add(slug)
                note(f'people/{slug}', ev)
    for c in hub.get('companies') or []:
        slug = maps['by_hub'].get(str(c.get('id')))
        if not slug:
            continue
        entry, p = comp_all[slug], c.get('properties') or {}
        dom = str(p.get('domain') or '').lower().removeprefix('www.')
        bits = [x for x in (G._clean(p.get('industry'), 40).replace('_', ' ').lower(),
                            ', '.join(filter(None, (G._clean(p.get('city'), 30), G._clean(p.get('state'), 20))))) if x]
        stage = str(p.get('lifecyclestage') or '')
        what = f"HubSpot lists {entry['name']}" + (f" ({dom})" if dom else '') + (': ' + '; '.join(bits) if bits else '') + \
               (f"; stage {stage}" if stage.isalpha() else '') + '.'
        ev = {'id': f'hubspot:company:{c.get("id")}:what', 'at': G._ts(p.get('hs_lastmodifieddate')) or now, 'source': 'hubspot',
              'where': 'the HubSpot company', 'text': what, 'claim': {'field': 'what', 'value': entry['name']},
              'ref': {'object': 'companies', 'id': str(c.get('id')), 'property': 'name', 'value': p.get('name')}, 'read_at': now}
        if _slot_add(entry, ev):
            touched['companies'].add(slug)
            note(f'companies/{slug}', ev)
    by_id = {str(c.get('id')): c for c in (hub.get('deal_contacts') or []) + (hub.get('contacts') or [])}
    on_pages = deals_on_pages(src_all, comp_all)
    for d in hub.get('deals') or []:
        why = G.why_not_deal(d, stages)
        if not why and str(d.get('id')) not in on_pages and str((d.get('properties') or {}).get('pipeline')) not in {str(x) for x in allowed}:
            why = 'a deal in a pipeline the pages hold no deal from'
        if why:
            stats[why] = stats.get(why, 0) + 1
            continue
        # where the seed files a deal: its own company, else the company of the contact on it (pages.plan)
        homes, direct = [str(x) for x in d.get('companies') or []], bool(d.get('companies'))
        persons = []
        for xid in d.get('contacts') or []:
            hp = (by_id.get(str(xid)) or {}).get('properties') or {}
            dom = str(hp.get('email') or '').lower().partition('@')[2]
            ps = maps['by_addr'].get(G.address_key(hp.get('email')))
            if not direct:
                if hp.get('associatedcompanyid') and str(hp['associatedcompanyid']) in maps['by_hub']:
                    homes.append(str(hp['associatedcompanyid']))
                elif dom and dom not in G.FREE_MAIL and dom in maps['by_domain']:
                    homes.append(str(comp_all[maps['by_domain'][dom]].get('hubspot_id')))
                elif hp.get('company'):
                    homes.append('name:' + G._norm(hp['company']))
                elif hp.get('associatedcompanyid'):
                    homes.append(str(hp['associatedcompanyid']))
                if ps:
                    persons.append(ps)
            if (not ps and hp.get('email') and not G.robot(hp.get('email'))) or not hp:
                touched['new_deals'].append(str(d.get('id')))   # someone on a changed real deal with no page yet
        if not d.get('contacts') and not homes:
            continue
        targets = [('companies', maps['by_hub'][h]) for h in dict.fromkeys(homes) if h in maps['by_hub']]
        if any(h not in maps['by_hub'] for h in homes):
            touched['new_deals'].append(str(d.get('id')))   # the deal's company has no page yet
        targets += [('people', s) for s in dict.fromkeys(persons)]
        did = str(d.get('id'))
        for slug, x in src_all.items():                    # and wherever this deal already is
            if ('people', slug) not in targets and any((e.get('ref') or {}).get('object') == 'deals' and str((e.get('ref') or {}).get('id')) == did
                                                       for e in x.get('evidence') or []):
                targets.append(('people', slug))
        for slug, x in comp_all.items():
            if ('companies', slug) not in targets and any((e.get('ref') or {}).get('object') == 'deals' and str((e.get('ref') or {}).get('id')) == did
                                                          for e in x.get('evidence') or []):
                targets.append(('companies', slug))
        for kind, slug in targets:
            ev = G._deal_line(d, stages, now)
            if _slot_add((src_all if kind == 'people' else comp_all)[slug], ev):
                touched[kind].add(slug)
                note(f'{kind}/{slug}', ev)
    return touched


# ---------- someone new: the seed's own bar, by the seed's own code ----------

def admit(queue: dict, deal_ids: list, mail, hub, index: dict, src_all: dict, comp_all: dict, now: float, added: list,
          since_at=None) -> dict:
    """The addresses in `queue` (the owner wrote to them and they wrote back) and the deals in `deal_ids` (real deals that
    changed and name someone new) are put to pages.plan, the seed's own decision, with the seed's own reads for just
    these addresses and just these deals. Never every deal in the portal: that would be a re-seed. Only pages that do
    not exist yet are taken from its answer; pages that exist stay with the daily fold.
    -> {'people', 'companies', 'filed': [slugs], 'declined': [address keys], 'skipped': plan's reasons}."""
    by_box = {}
    for k, c in queue.items():
        if c.get('box') in BOXES:
            by_box.setdefault(c['box'], []).append(c['address'])
    mail_by_box, heard = {}, {}
    for box, addrs in by_box.items():
        mail_by_box[box] = mail.sent_since(box, (since_at or now) - BAR_DAYS * 86400, None, 60, to=sorted(addrs))[0]
        for a, h in mail.heard_from(box, sorted(addrs), BAR_DAYS).items():
            k = G.address_key(a)
            if k not in heard or (h.get('at') or 0) > (heard[k].get('at') or 0):
                heard[k] = {**h, 'mailbox': box}
    hub_data = hub.read(sorted({c['address'] for c in queue.values()}), deal_ids=sorted(set(deal_ids))) if hub is not None else {}
    got = G.plan(mail_by_box, heard, hub_data, index, src_all, comp_all, now, {'per_mailbox': 150, 'people': NEW_PAGES, 'companies': NEW_PAGES})
    touched = {'people': set(), 'companies': set(), 'filed': [], 'declined': [], 'skipped': got.get('skipped') or {}}

    def note(page, ev):
        added.append({'page': page, 'id': ev['id'], 'at': ev.get('at'), 'where': ev.get('where'), 'text': ev.get('text')})

    for slug, e in got['people'].items():
        if slug in src_all or slug in index:
            have = {G.address_key(a) for a in (src_all.get(slug) or {}).get('emails') or []}
            more = [a for a in e['emails'] if G.address_key(a) not in have]
            if slug in src_all and more:                   # the same person at another address (pages.person_id): the
                keep = src_all[slug]                       # address and its mail join the one record, never a second page
                keep['emails'] = sorted(set(keep.get('emails') or []) | set(more))
                for ev in e['evidence']:
                    if G._add(keep, ev):
                        note(f'people/{slug}', ev)
                touched['people'].add(slug)
            continue                                       # a page that exists is never rebuilt here
        keep = src_all.setdefault(slug, {k: v for k, v in e.items() if k != 'evidence'} | {'evidence': []})
        keep['emails'] = sorted(set(e['emails']))
        for ev in e['evidence']:
            if G._add(keep, ev):
                note(f'people/{slug}', ev)
        touched['people'].add(slug)
        touched['filed'].append(f'people/{slug}')
    for slug, c in got['companies'].items():
        if slug in comp_all:
            keep = comp_all[slug]
            fresh = [s for s in c['people'] if s in touched['people'] and s not in (keep.get('people') or [])]
            if not fresh:
                continue
            keep['people'] = sorted(set(keep.get('people') or []) | set(fresh))
            touched['companies'].add(slug)
            names = {src_all[s]['name'] for s in fresh}
            for ev in c['evidence']:                       # only the new person's mail; this page's HubSpot lines stay with the daily fold
                ref = ev.get('ref') or {}
                if ev.get('source') == 'email' and any(n in str(ev.get('text')) for n in names) and ev['id'] not in _ids(keep) \
                        and not _company_has(keep, ref.get('mailbox'), ref.get('subject'), ev.get('at') or 0, False):
                    if G._add(keep, ev):
                        note(f'companies/{slug}', ev)
            continue
        keep = comp_all.setdefault(slug, {k: v for k, v in c.items() if k != 'evidence'} | {'evidence': []})
        keep['people'] = sorted(s for s in c['people'] if s in src_all)
        for ev in c['evidence']:
            if G._add(keep, ev):
                note(f'companies/{slug}', ev)
        touched['companies'].add(slug)
        touched['filed'].append(f'companies/{slug}')
        for s in keep['people']:
            touched['people'].add(s)                       # their page now names the company
    have = {G.address_key(a) for x in src_all.values() for a in x.get('emails') or []}
    touched['declined'] = sorted(k for k in queue if k not in have)
    return touched


# ---------- writing ----------

def _write_out(life: Path, index: dict, src_all: dict, comp_all: dict, people: set, companies: set, added: list) -> list:
    """Save the two stores and write again ONLY the pages that got something. -> the pages written."""
    if not people and not companies:
        return []
    try:
        C = __import__('claims')                           # claims.py (switch claims-on): each NEW line says who it is from and how it was read
        ids = {(a['page'], a['id']) for a in added}
        for kind, store in (('people', src_all), ('companies', comp_all)):
            for slug in (people if kind == 'people' else companies):
                for ev in (store.get(slug) or {}).get('evidence') or []:
                    if (f'{kind}/{slug}', ev.get('id')) in ids:
                        C.stamp(life, 'page', ev, owner=str(store[slug].get('name') or slug))
    except Exception:
        pass
    if people:
        P._write(P._dir(life) / 'sources.json', json.dumps(src_all, indent=2, ensure_ascii=False, sort_keys=True))
    if companies:
        P._write(life / 'companies' / 'index.json', json.dumps(comp_all, indent=2, ensure_ascii=False, sort_keys=True))
    cnames = {s: c.get('name') for s, c in comp_all.items()}
    pnames = {s: (index.get(s) or src_all.get(s) or {}).get('name') for s in set(index) | set(src_all)}
    wrote = []
    for slug in sorted(people):
        if slug in index and not G.on(life):
            continue                                       # the owner's own pages keep people.py's layout without pages-on
        name, person, src, _ = G._entry_parts(life, 'person', slug, index, src_all, comp_all)
        text = G.render('person', name, person, src, names_of=cnames)
        f = P._dir(life) / f'{slug}.md'
        if not f.exists() or f.read_text(encoding='utf-8') != text:
            P._write_quiet(f, text)                        # a line from mail research already saw is not a new reason to wake it
            wrote.append(f'people/{slug}')
    for slug in sorted(companies):
        c = comp_all[slug]
        text = G.render('company', c.get('name') or slug, None, None, c, names_of=pnames)
        f = life / 'companies' / f'{slug}.md'
        if not f.exists() or f.read_text(encoding='utf-8') != text:
            P._write(f, text)
            wrote.append(f'companies/{slug}')
    return wrote


# ---------- the daily run ----------

def touched(life, since: float) -> list:
    """The inboxes that got new mail after `since`, from the engine's own record of every email it has seen
    (the ledger's `seen` table, ids "email:<inbox>:<id>", written by the tick). Nothing is read from mail to find
    this out, and this file still never opens the ledger itself (people.ledger_rows, read-only). [] when nothing came
    in, or when the ledger is missing or cannot be read (the day's first pass still covers everything)."""
    ids = [r[0] for r in P.ledger_rows(life, "SELECT id FROM seen WHERE id LIKE 'email:%' AND CAST(at AS REAL) > ?", (float(since),)) or []]
    return [b for b in BOXES if any(str(i).startswith(f'email:{b}:') for i in ids)]


def due(st: dict, now_dt: datetime, life=None) -> str:
    """'new' (today's run starts), 'resume' (steps left from a run that ran out of time), 'retry' (a failed read, two
    hours on), 'more' (today's first pass is done, an hour has gone by and an inbox got new mail), or why not:
    'early' (before 6:00am), 'already' (nothing new since the last pass, or it was under an hour ago), 'waiting' (a
    failed read, not two hours ago yet). The hourly 'more' pass keeps a reply from waiting until the next morning to
    reach the sender's page."""
    run = st.get('run') or {}
    if run.get('day') == now_dt.date().isoformat():
        if not run.get('todo'):
            last = float(run.get('started') or 0)
            return 'more' if life is not None and now_dt.timestamp() - last >= PASS_EVERY and touched(life, last) else 'already'
        if run.get('retry_at'):
            return 'retry' if now_dt.timestamp() >= float(run['retry_at']) else 'waiting'
        return 'resume'
    return 'early' if now_dt.hour < START_HOUR else 'new'


def run(life, now_dt: datetime, mail=None, hub=None, *, budget=BUDGET_S, hard=HARD_S, clock=time.monotonic, force=False,
        until=None) -> dict:
    """One slice of the daily pass. The caller holds the tick's lock. mail / hub: readers (Mail, Hub above, or fakes).
    force: run now whatever the hour and whatever today's run did. until: read no later than this time (replays).
    -> {'status': 'off' | 'not seeded' | 'early' | 'already' | 'waiting' | 'ok' | 'partial' | 'failed' | 'cut', ...}."""
    life = Path(life)
    if not on(life):
        return {'status': 'off'}
    seeded = _seed_at(life)
    if seeded is None:
        return {'status': 'not seeded'}                    # never a re-seed: with no seed there is nothing to keep fresh
    st = load_state(life)
    why = 'new' if force else due(st, now_dt, life)
    if why in ('early', 'already', 'waiting'):
        return {'status': why}
    t0 = clock()
    now = now_dt.timestamp()
    mail = mail or Mail(deadline=t0 + hard, clock=clock)
    paused = getattr(hub, 'portal_id', G.HUBSPOT_PORTAL if hub is None else None) in G.PAUSED_PORTALS
    hub = None if paused else (hub or Hub(deadline=t0 + hard, clock=clock))
    st.setdefault('since', {})
    st.setdefault('could_not_read', {})
    st.setdefault('pending', {})
    st.setdefault('admit', {})
    st.setdefault('declined', {})
    for k in ('window', 'behind', 'upto', 'high'):
        st.setdefault(k, {})
    memo = _load_memo(life)
    for rd in (mail, hub):
        if hasattr(rd, 'memo'):
            rd.room = MEMO_ROOM - len(json.dumps(memo, ensure_ascii=False))
    if why == 'new':
        st['run'] = {'day': now_dt.date().isoformat(), 'started': now, 'until': float(until or now), 'attempt': 1, 'retry_at': None,
                     'todo': [f'mail:{b}' for b in BOXES] + ['hubspot', 'new'], 'failed': {}, 'added': 0}
        st['capped'] = {}
    elif why == 'more':                                    # only the inboxes with new mail, then anyone new who wrote back
        st['run'].update(todo=[f'mail:{b}' for b in touched(life, float(st['run'].get('started') or 0))] + ['new'],
                         started=now, until=float(until or now), attempt=1, retry_at=None, failed={})
    elif why == 'retry':
        st['run'].update(until=float(until or now), attempt=int(st['run'].get('attempt') or 1) + 1, retry_at=None, failed={})
    r = st['run']
    if paused:
        if 'hubspot' not in st.get('paused_sources', {}):
            memo.pop('new', None)
        st['paused_sources'] = {'hubspot': 'HubSpot is not configured' if not G.HUBSPOT_ON else 'HubSpot is paused'}
        r['todo'] = [s for s in r['todo'] if s != 'hubspot']
        for section in ('could_not_read', 'behind', 'capped'):
            st.get(section, {}).pop('hubspot', None)
        r['failed'].pop('hubspot', None)
        # Keep the portal cursor and its outstanding deals for an explicitly authorized resumption.
        # Do not reuse cached CRM responses for the parallel mail-only discovery step.
    else:
        st.pop('paused_sources', None)
    index = P.load(life)
    src_all, comp_all = G.load_sources(life), G.load_companies(life)
    added, stats, wrote, again, cut = [], {}, [], [], False
    for step in list(r['todo']):
        if clock() - t0 >= budget:
            cut = True
            break
        people, companies = set(), set()
        for rd in (mail, hub):
            if hasattr(rd, 'memo'):
                rd.memo = memo.setdefault(step, {})        # the answers this step already got on an earlier slice
        behind = False                                     # the step read all it could, but there is more: it stays on the list
        try:
            if step.startswith('mail:'):
                box = step[5:]
                win = st['window'].get(step)
                if not win:                                # the window stays fixed until it is read through (the reads are remembered by it)
                    since = float(st['since'].get(step) or _first_since(seeded))
                    top = float(st['upto'].get(step) or r['until'])
                    frm = since - OVERLAP
                    if top - frm > LOOK_BACK_CAP:
                        frm = top - LOOK_BACK_CAP
                        st['capped'][step] = {'at': now, 'why': 'looked back 30 days only'}
                    win = st['window'][step] = {'from': frm, 'until': top}
                frm, top = float(win['from']), float(win['until'])
                maps = _maps(src_all, comp_all)
                known = sorted(a.lower() for x in src_all.values() for a in x.get('emails') or [])
                if len(known) > 2 * GROUP:                 # many pages: each inbox is asked only about the people seen in it
                    known = sorted({a.lower() for x in src_all.values() for a in x.get('emails') or []
                                    if not any(e.get('source') == 'email' for e in x.get('evidence') or [])
                                    or any((e.get('ref') or {}).get('mailbox') == box for e in x.get('evidence') or [])})
                waiting = sorted(c['address'] for c in st['pending'].values() if c.get('box') == box)
                sent, more = mail.sent_since(box, frm, top, SENT_CAP)
                heard, more2 = mail.received_since(box, known + waiting, frm, top, HEARD_CAP) if known or waiting else ([], False)
                sent = [m for m in sent if (m.get('at') or 0) < top]           # the window ends just before `top`: what is at
                heard = [h for h in heard if (h.get('at') or 0) < top]         # or after it was read by the slice before
                heard = _with_words(mail, box, heard, src_all)                 # the body behind each new line
                keep_from = None
                if more or more2:                          # more than one read holds: take only what was read in full, oldest end next
                    keep_from = _read_down_to(sent, more, heard, more2, frm, top)
                    if keep_from is None:
                        st['capped'][step] = {'at': now, 'why': 'more new mail than one run reads'}
                    else:
                        sent = [m for m in sent if (m.get('at') or 0) >= keep_from]
                        heard = [h for h in heard if (h.get('at') or 0) >= keep_from]
                got = fold_mail(box, sent, heard, src_all, comp_all, now, added, stats)
                new = {k: c for k, c in got['new'].items() if now - float(st['declined'].get(k) or 0) > DECLINED_DAYS * 86400}
                for k, c in st['pending'].items():         # people the owner wrote to who were not checked yet (more than one run checks)
                    if c.get('box') == box and c.get('unchecked') and k not in new:
                        new[k] = c
                order = sorted(new.items(), key=lambda kv: (not kv[1].get('unchecked'), -float(kv[1].get('at') or 0)))
                check, rest = dict(order[:NEW_CAP]), dict(order[NEW_CAP:])
                back = mail.heard_from(box, sorted(c['address'] for c in check.values()), BAR_DAYS) if check else {}
                wrote_back = {G.address_key(a) for a in back} | {h['from'] for h in heard if h.get('from') not in maps['by_addr']}
                for k, c in check.items():
                    c = {x: v for x, v in c.items() if x != 'unchecked'}
                    if k in wrote_back:
                        st['pending'].pop(k, None)
                        st['admit'][k] = c                 # the owner wrote, and they wrote back: the seed's bar decides (pages.plan)
                    else:
                        st['pending'][k] = c
                for k, c in rest.items():
                    st['pending'][k] = {**c, 'unchecked': True}
                if rest:
                    st['capped']['new'] = {'at': now, 'why': f'{len(rest)} addresses wait to be checked'}
                for k in [k for k in st['pending'] if k in wrote_back]:
                    st['admit'][k] = {x: v for x, v in st['pending'].pop(k).items() if x != 'unchecked'}
                people, companies = got['people'], got['companies']
                st['window'].pop(step, None)
                if keep_from is not None:                  # the cursor moves only past what was read: the older part is next
                    st['high'].setdefault(step, top)
                    st['upto'][step] = keep_from
                    st['behind'][step] = st['behind'].get(step) or {'since': now, 'why': 'more'}
                    behind = True
                else:
                    st['since'][step] = float(st['high'].pop(step, None) or top)
                    st['upto'].pop(step, None)
                    behind = st['since'][step] < r['until'] - 1
            elif step == 'hubspot':
                win = st['window'].get(step)
                if not win:
                    since = float(st['since'].get(step) or _first_since(seeded))
                    win = st['window'][step] = {
                        'from': max(since - OVERLAP, r['until'] - LOOK_BACK_CAP), 'until': r['until'],
                        'emails': sorted({a.lower() for x in src_all.values() for a in x.get('emails') or []}),
                        'ids': sorted({str(c.get('hubspot_id')) for c in comp_all.values() if c.get('hubspot_id')})}
                data = hub.changed(float(win['from']), float(win['until']), set(win['emails']), set(win['ids']))
                have = deals_on_pages(src_all, comp_all)
                unfiled = [d for d in data.get('deals') or [] if str(d.get('id')) not in have and not G.why_not_deal(d, data.get('stages') or {})]
                allowed = set(hub.pipelines_of(sorted(have)).values()) if unfiled and have else set()
                got = fold_hub(data, src_all, comp_all, now, added, stats, allowed)
                st['admit_deals'] = sorted(set(st.get('admit_deals') or []) | set(got['new_deals']))
                people, companies = got['people'], got['companies']
                st['window'].pop(step, None)
                st['since'][step] = float(win['until'])
                behind = st['since'][step] < r['until'] - 1    # an older window was read through; the newer part is next
            else:                                          # 'new': someone new, by the seed's own bar
                win = st['window'].get(step)
                eligible_deals = [] if paused else st.get('admit_deals') or []
                if not win and (st['admit'] or eligible_deals):
                    win = st['window'][step] = {
                        'at': now, 'batch': [k for k, _ in sorted(st['admit'].items(), key=lambda kv: -float(kv[1].get('at') or 0))[:NEW_PAGES]],
                        'deals': sorted(eligible_deals)[:DEALS_PER_RUN]}
                if win:
                    batch = {k: st['admit'][k] for k in win['batch'] if k in st['admit']}
                    deals = [] if paused else list(win['deals'])
                    got = admit(batch, deals, mail, hub, index, src_all, comp_all, now, added, since_at=float(win['at']))
                    for k in got['declined']:
                        st['declined'][k] = now
                    for k in batch:
                        st['admit'].pop(k, None)
                        st['pending'].pop(k, None)
                    st['admit_deals'] = [d for d in st.get('admit_deals') or [] if d not in deals]
                    over = {why: n for why, n in got['skipped'].items() if why.startswith('over the ')}
                    stats.update(over)
                    if st['admit'] or (not paused and st['admit_deals']) or over:   # more new than one run adds: they wait for the next run, and that is said
                        st['capped']['new'] = {'at': now, 'why': f"{len(st['admit'])} addresses and {len(st['admit_deals'])} deals wait for the next run"}
                    r['filed'] = sorted(set(r.get('filed') or []) | set(got['filed']))
                    people, companies = got['people'], got['companies']
                    st['window'].pop(step, None)
        except OutOfTime:
            src_all, comp_all = G.load_sources(life), G.load_companies(life)     # nothing of a cut step is kept but its reads
            added[:] = [a for a in added if a.get('step') is not None]
            st['behind'][step] = st['behind'].get(step) or {'since': now, 'why': 'slow'}
            cut = True
            break
        except Exception as e:
            src_all, comp_all = G.load_sources(life), G.load_companies(life)     # nothing of a failed step is kept
            added[:] = [a for a in added if a.get('step') is not None]
            r['failed'][step] = f'{type(e).__name__}: {str(e)[:120]}'
            bad = st['could_not_read'].setdefault(step, {'since': now})
            bad.update(last=now, why=r['failed'][step])
            st['window'].pop(step, None)                   # a retry reads afresh; an older-mail backlog (upto) is kept
            memo.pop(step, None)
            again.append(step)
            r['todo'].remove(step)
            continue
        for a in added:
            a.setdefault('step', step)
        wrote += _write_out(life, index, src_all, comp_all, people, companies, [a for a in added if a['step'] == step])
        memo.pop(step, None)                               # the window was read through: its remembered answers go
        st['could_not_read'].pop(step, None)
        if behind:
            st['behind'].setdefault(step, {'since': now, 'why': 'slow'})
            r['todo'].remove(step)
            r['todo'].append(step)                         # the rest of it on the next engine run, after the other steps
        else:
            st['behind'].pop(step, None)
            r['todo'].remove(step)
        r['added'] = int(r.get('added') or 0) + sum(1 for a in added if a['step'] == step)
        _tidy(st, now)
        st['partial'] = sentences(st)
        _save_state(life, st)                              # the cursor: a run stopped here picks up at the next step
        _save_memo(life, memo)
    status = 'cut'
    if not cut:
        behind_left = [s for s in r['todo'] if s not in again]
        r['todo'] = behind_left + again
        if again:
            r['retry_at'] = now + RETRY_AFTER
            status = 'failed' if len(again) >= len(BOXES) + 1 else 'partial'
        elif behind_left:
            status = 'partial'                             # more to read than this run held; the next engine run goes on
        else:
            status = 'ok'
            st['last_good'] = r['until']
    st['status'], st['last_try'] = status, now
    st['partial'] = sentences(st)
    _save_state(life, st)
    _save_memo(life, memo)
    rep = {'status': status, 'day': r['day'], 'attempt': r.get('attempt'), 'lines_added': len(added), 'pages_written': wrote,
           'steps_left': list(r['todo']), 'could_not_read': sorted(st['could_not_read']), 'partial': st['partial'],
           'reads': {'gmail': getattr(mail, 'calls', None), 'hubspot': getattr(hub, 'calls', None)},
           'left_out': stats, 'seconds': round(clock() - t0, 1)}
    if r.get('filed'):
        rep['new_pages'] = r['filed']
    try:
        with open(life / LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({**{k: v for k, v in rep.items() if k != 'partial'}, 'at': now,
                                 'added': [{k: a.get(k) for k in ('page', 'id', 'at', 'where', 'step')} for a in added]}, ensure_ascii=False) + '\n')
    except OSError:
        pass
    rep['added'] = added
    return rep


def _tidy(st: dict, now: float):
    """The people the owner wrote to who have not written back: 30 days at most, PENDING_MAX at most. Kept first: the ones
    not checked yet, then the ones with a name (the seed files nobody it cannot name), then the newest. Anyone let go
    for room (not for age) is said: the state turns partial."""
    keep = {k: c for k, c in st['pending'].items() if now - float(c.get('at') or 0) <= PENDING_DAYS * 86400
            and not G.robot(c.get('address') or k, c.get('name') or '')}     # a robot the filters know leaves the waiting list

    def rank(kv):
        c = kv[1]
        return (not c.get('unchecked'), not G.person_name(c.get('name') or '', c.get('address') or kv[0]), -float(c.get('at') or 0))
    st['pending'] = dict(sorted(keep.items(), key=rank)[:PENDING_MAX])
    if len(keep) > PENDING_MAX:
        st.setdefault('capped', {})['new'] = {'at': now, 'why': f'{len(keep) - PENDING_MAX} people {OWNER} wrote to were let go for room'}
    st['declined'] = {k: t for k, t in st['declined'].items() if now - float(t) <= DECLINED_DAYS * 86400}


def tend(life, now_dt: datetime, **stand_ins) -> dict:
    """Call from every tick, with the other once-a-day passes. Never sends. Own guard: nothing here can stop a tick."""
    try:
        rep = run(life, now_dt, **stand_ins)
        return {k: v for k, v in rep.items() if k != 'added'}
    except Exception as e:
        return {'status': 'error', 'why': f'{type(e).__name__}: {str(e)[:160]}'}


def health_part(life, note: dict) -> None:
    """A small `pages_fresh` part for health.json. Never raises."""
    try:
        st = load_state(life)
        note['pages_fresh'] = {'paused_sources': st.get('paused_sources') or {}, 'status': st.get('status') or 'not run yet', 'last_good': st.get('last_good'), 'last_try': st.get('last_try'),
                               'could_not_read': sorted(st.get('could_not_read') or {}), 'partial': st.get('partial') or [],
                               'steps_left': list((st.get('run') or {}).get('todo') or [])}
    except Exception as e:
        note['pages_fresh'] = {'error': type(e).__name__}


# ---------- hand tool ----------

def main(argv=None):
    """run [--force] | status   (life folder: LIFE_HOME or --life-home). `run` takes the tick's own lock."""
    a = list(sys.argv[1:] if argv is None else argv)
    home = a[a.index('--life-home') + 1] if '--life-home' in a and a.index('--life-home') + 1 < len(a) else os.environ.get('LIFE_HOME')
    if not home or not a:
        print(main.__doc__)
        return 2
    if a[0] == 'status':
        st = load_state(home)
        print(json.dumps({k: st.get(k) for k in ('status', 'last_good', 'last_try', 'since', 'could_not_read', 'partial', 'run')}, indent=1, ensure_ascii=False))
        return 0
    if a[0] == 'run':
        lock = G._lock(Path(home))
        if lock is None:
            print('Not run: a tick is running right now; try again in a minute.')
            return 1
        try:
            from zoneinfo import ZoneInfo
            rep = run(home, datetime.now(ZoneInfo(settings.OWNER_TZ)), force='--force' in a, budget=600, hard=900)
        finally:
            lock.close()
        print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        return 0
    print(main.__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
