"""booking: a booking yes binds the trip, not just store + total.

Why. A check of only the store and the total would let a flight on the wrong date, the wrong flight, or for the wrong
passenger at the same price pass. So the ask reads back the item, date and time, party size, total and cancellation
terms, and a datetime that never appeared is refused.

What this module does (stdlib only; used by buyfence in the browser child and by zeus-buy):
  * read_page()      reads a booking store's final review / purchase page: flight numbers, the date of each flight,
                     times near them, airport codes, passenger names (page text AND the name boxes on the form) and
                     how many passengers, fare names and refund words. Patterns come from stores.json (the store's
                     "booking" block plus generic "airports").
  * compare()        the owner's recorded trip (the grant's "booking") against what the page shows. Any detail that
                     differs or is missing is named in plain words. No flight number and no date at all = unreadable.
  * missing_in_ask() which trip details Zeus's ask (the message the owner said yes to) did not name. A detail the
                     owner never saw cannot be part of their yes.
  * validate()       the trip details given to `zeus-buy yes` (--date --flights --from --to --passengers [--time]
                     [--fare]).
  * ask_line(), cmd_flags(), describe(), from_page(): the plain texts.

Only kind "flight" is built. A store marked with another kind fails closed (no booking can pass) until code for it
exists. Real airline page wording varies: every pattern here is general knowledge, made tolerant, and a page that
cannot be read refuses.
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata

VERSION = 'purchase-lock-2'
H = r'[ \t ]'                                   # horizontal space only: never join two lines into one detail
WEEKDAYS = {'monday': 0, 'mon': 0, 'tuesday': 1, 'tues': 1, 'tue': 1, 'wednesday': 2, 'weds': 2, 'wed': 2, 'thursday': 3,
            'thurs': 3, 'thur': 3, 'thu': 3, 'friday': 4, 'fri': 4, 'saturday': 5, 'sat': 5, 'sunday': 6, 'sun': 6}
MONTHS = {'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3, 'april': 4, 'apr': 4, 'may': 5, 'june': 6,
          'jun': 6, 'july': 7, 'jul': 7, 'august': 8, 'aug': 8, 'september': 9, 'sept': 9, 'sep': 9, 'october': 10, 'oct': 10,
          'november': 11, 'nov': 11, 'december': 12, 'dec': 12}
WD_ABBR = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MO_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
_WD = '|'.join(sorted(WEEKDAYS, key=len, reverse=True))
_MO = '|'.join(sorted(MONTHS, key=len, reverse=True))
_PFX = rf'(?:\b({_WD})\b\.?,?{H}+)?'                 # optional weekday in front: "Sat, ", "Saturday "
DATE_RXS = (
    ('iso', re.compile(r'(?<![\d-])(20\d\d)-(\d{1,2})-(\d{1,2})(?![\d-])')),
    ('month_day', re.compile(rf'(?i:{_PFX}\b({_MO})\b\.?{H}*(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?!:)(?:,?{H}+(\d{{4}})(?!\d))?)')),
    ('day_month', re.compile(rf'(?i:{_PFX}(?<![\d/])(\d{{1,2}})(?:st|nd|rd|th)?{H}+({_MO})\b\.?(?:,?{H}+(\d{{4}})(?!\d))?)')),
    ('numeric', re.compile(rf'(?i:{_PFX})(?<![\d/.$#])(\d{{1,2}})/(\d{{1,2}})(?:/(\d{{4}}|\d{{2}}))?(?![\d/])')),
)
TIME_12 = re.compile(rf'(?<![\d:])(\d{{1,2}}):([0-5]\d){H}?([AaPp])\.?{H}?[Mm]\.?(?![a-z])')
TIME_24 = re.compile(r'(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])')
CODE = re.compile(r'(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])')
SUFFIX = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}
NAME_WORD = r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’.\-]*"
NAME_LINE = re.compile(rf'^{NAME_WORD}(?:{H}+{NAME_WORD}){{1,3}},?$')
SLASH_NAME = re.compile(rf'^({NAME_WORD}(?:{H}+{NAME_WORD})?){H}*/{H}*({NAME_WORD}(?:{H}+{NAME_WORD}){{0,2}})$')
COMMA_NAME = re.compile(rf'^({NAME_WORD}(?:{H}+{NAME_WORD})?),{H}*({NAME_WORD}(?:{H}+{NAME_WORD}){{0,2}})$')


class BookingError(ValueError):
    """A booking store's settings cannot be used (unknown kind, bad pattern). Callers fail closed."""


# ---- settings ---------------------------------------------------------------------------------------------------------

def prepare(raw: dict, airports: dict | None = None) -> dict:
    """The store's "booking" block from stores.json, with its patterns compiled. Raises BookingError for a kind this
    code cannot check (so no booking at that store can pass)."""
    if not isinstance(raw, dict):
        raise BookingError('the booking settings are not a table')
    kind = raw.get('kind')
    if kind != 'flight':
        raise BookingError(f'booking kind "{kind}" has no checker yet')
    try:
        anchor = raw['flight_anchor']
        ask_anchor = anchor + ('|' + raw['ask_anchor_extra'] if raw.get('ask_anchor_extra') else '')
        tail = rf'(\d{{1,4}})(?!\d)((?:{H}*/{H}*(?:{anchor})?\d{{1,4}}(?!\d))*)(?!{H}*of{H}+\d)'
        cfg = dict(raw)
        cfg['_flight_rx'] = re.compile(rf'(?:{anchor}){tail}')
        cfg['_ask_flight_rx'] = re.compile(rf'(?:{ask_anchor}){tail}')
        cfg['_hash_skip'] = re.compile(raw.get('hash_skip_before') or r'(?!x)x', re.I)
        p = raw.get('passengers') or {}
        cfg['_pax_label'] = re.compile(p['label'], re.I)
        cfg['_pax_counts'] = [re.compile(x, re.I) for x in p.get('count', [])]
        cfg['_pax_skip'] = re.compile(p.get('skip_line') or r'(?!x)x', re.I)
        cfg['_stop'] = {w.lower() for w in p.get('not_name_words', [])}
        cfg['_fares'] = [(f['name'], re.compile(f['pattern']), re.compile(f['pattern'], re.I)) for f in raw.get('fares', [])]
        cfg['_refund'] = [(re.compile(r['pattern']), r['say']) for r in raw.get('refund_terms', [])]
    except (KeyError, TypeError, re.error) as error:
        raise BookingError(f'the booking settings are broken ({type(error).__name__}: {error})') from error
    cfg['airports'] = {str(k).upper(): str(v) for k, v in (airports or {}).items()}
    cfg['airline_code'] = raw.get('airline_code') or ''
    cfg['button'] = raw.get('button') or 'Purchase'
    return cfg


def today() -> _dt.date:
    return _dt.date.today()


# ---- dates, times, words ----------------------------------------------------------------------------------------------

def _year(y) -> int | None:
    if not y:
        return None
    y = int(y)
    return y + 2000 if y < 100 else y


def _infer(m: int, d: int, weekday, year, ref: _dt.date):
    """-> (date or None, conflict). A weekday that does not fit the date is a conflict: that date never appeared."""
    wd = WEEKDAYS.get(str(weekday).lower()) if weekday else None
    y = _year(year)
    if y:
        try:
            x = _dt.date(y, m, d)
        except ValueError:
            return None, False
        return x, (wd is not None and x.weekday() != wd)
    cands = []
    for k in (0, 1):
        try:
            cands.append(_dt.date(ref.year + k, m, d))
        except ValueError:
            pass
    if not cands:
        return None, False
    if wd is not None:
        fit = [c for c in cands if c.weekday() == wd]
        if not fit:
            return cands[0], True
        cands = fit
    ahead = [c for c in cands if c >= ref - _dt.timedelta(days=2)]
    return (ahead[0] if ahead else cands[-1]), False


def parse_dates(text: str, ref: _dt.date | None = None) -> list:
    """Every date written in the text, in any common form ('Sat, Mar 2', 'Saturday, March 2, <year>', '03/02/<year>',
    '03/02/<yy>', ISO YYYY-MM-DD, '2 Mar'). -> [{start, end, date, conflict}] in text order, no overlaps."""
    ref = ref or today()
    hits = []
    for kind, rx in DATE_RXS:
        for m in rx.finditer(text or ''):
            g = m.groups()
            if kind == 'iso':
                wd, mo, dy, yr = None, int(g[1]), int(g[2]), g[0]
            elif kind == 'month_day':
                wd, mo, dy, yr = g[0], MONTHS[g[1].lower()], int(g[2]), g[3]
            elif kind == 'day_month':
                wd, mo, dy, yr = g[0], MONTHS[g[2].lower()], int(g[1]), g[3]
            else:
                wd, mo, dy, yr = g[0], int(g[1]), int(g[2]), g[3]
            if not (1 <= mo <= 12 and 1 <= dy <= 31):
                continue
            date, conflict = _infer(mo, dy, wd, yr, ref)
            if date is None:
                continue
            hits.append({'start': m.start(), 'end': m.end(), 'date': date, 'conflict': conflict})
    hits.sort(key=lambda h: (h['start'], -(h['end'] - h['start'])))
    out, last_end = [], -1
    for h in hits:
        if h['start'] >= last_end:
            out.append(h)
            last_end = h['end']
    return out


def parse_times(text: str, allow_24h: bool = True) -> list:
    """'5:30AM', '5:30 am', '5:30 a.m.', and (allow_24h) '17:30'. -> [{start, hm: 'HH:MM'}] in text order."""
    out, taken = [], set()
    for m in TIME_12.finditer(text or ''):
        h, mi = int(m.group(1)), int(m.group(2))
        if not 1 <= h <= 12:
            continue
        h = (h % 12) + (12 if m.group(3).lower() == 'p' else 0)
        out.append({'start': m.start(), 'hm': f'{h:02d}:{mi:02d}'})
        taken.add(m.start())
    if allow_24h:
        for m in TIME_24.finditer(text or ''):
            if m.start() in taken or any(o['start'] <= m.start() < o['start'] + 8 for o in out):
                continue
            out.append({'start': m.start(), 'hm': f'{int(m.group(1)):02d}:{m.group(2)}'})
    return sorted(out, key=lambda x: x['start'])


def date_words(iso: str, ref: _dt.date | None = None) -> str:
    """An ISO date (YYYY-MM-DD) -> 'Sat Mar 2' (the year is added when it is not this year)."""
    try:
        d = _dt.date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    ref = ref or today()
    return f'{WD_ABBR[d.weekday()]} {MO_ABBR[d.month - 1]} {d.day}' + (f' {d.year}' if d.year != ref.year else '')


def time_words(hm: str) -> str:
    """'05:30' -> '5:30am', '18:10' -> '6:10pm'."""
    try:
        h, m = (int(x) for x in str(hm).split(':'))
    except ValueError:
        return str(hm)
    return f"{h % 12 or 12}:{m:02d}{'am' if h < 12 else 'pm'}"


def and_join(items) -> str:
    items = [str(i) for i in items if str(i)]
    if len(items) <= 1:
        return ''.join(items)
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def flight_label(cfg: dict, digits: str) -> str:
    code = cfg.get('airline_code') or ''
    return f'{code} {digits}'.strip()


def city(cfg: dict, code: str | None) -> str:
    return cfg['airports'].get(str(code or '').upper(), '') if code else ''


# ---- names ------------------------------------------------------------------------------------------------------------

def _plain(s: str) -> str:
    s = unicodedata.normalize('NFKD', str(s or ''))
    return ''.join(c for c in s if not unicodedata.combining(c)).replace('’', "'")


def name_tokens(name: str) -> list:
    toks = [t.replace("'", '') for t in re.findall(r"[a-z]+(?:['\-][a-z]+)*", _plain(name).lower())]
    while len(toks) > 2 and toks[-1] in SUFFIX:
        toks.pop()
    return toks


def first_last(name: str):
    t = name_tokens(name)
    return (t[0], t[-1]) if len(t) >= 2 else None


def same_person(a: str, b: str) -> bool:
    fa, fb = first_last(a), first_last(b)
    return bool(fa and fb and fa == fb)


def name_in_text(name: str, text_plain_lower: str) -> bool:
    """Is this full name written in the (accent-free, lower-case) text? 'Alex Rivera' matches 'ALEX RIVERA',
    'Alex J. Rivera', 'Rivera, Alex' and 'RIVERA/ALEX'."""
    fl = first_last(name)
    if not fl:
        return False
    f, l_ = (re.escape(x).replace("\\-", "[-' ]?") for x in fl)
    rx = re.compile(rf"(?<![a-z]){f}(?![a-z])(?:[ \t.\-]+[a-z'.\-]+){{0,2}}[ \t]+{l_}(?![a-z])"
                    rf"|(?<![a-z]){l_}[ \t]*[,/][ \t]*{f}(?![a-z])")
    return bool(rx.search(text_plain_lower.replace("'", '')))


def _kind_of(s: str):
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(s or ''))
    s = re.sub(r'[_\-]+', ' ', s).lower()
    if re.search(r'\b(?:given name|first)\b', s):
        return 'first'
    if re.search(r'\b(?:additional name|middle)\b', s):
        return 'middle'
    if re.search(r'\b(?:family name|last|surname)\b', s):
        return 'last'
    if re.search(r'\bname\b', s):
        return 'full'
    return None


def field_kind(f: dict):
    """first | middle | last | full | None, from the box's own words first (autocomplete, label), then name/id, then
    the words around it."""
    for src in (f.get('ac'), ' '.join(str(f.get(k) or '') for k in ('lab', 'aria', 'ph')),
                ' '.join(str(f.get(k) or '') for k in ('name', 'id')), f.get('parent')):
        k = _kind_of(src)
        if k:
            return k
    return None


def _is_name_line(cfg: dict, line: str) -> str | None:
    """A line that is a person's name -> the name as 'First Last'; else None."""
    s = ' '.join(str(line or '').split())
    if not s or len(s) > 60 or re.search(r'\d', s):
        return None
    m = SLASH_NAME.match(s) or COMMA_NAME.match(s)
    if m:
        s = f'{m.group(2)} {m.group(1)}'
    elif not NAME_LINE.match(s):
        return None
    words = name_tokens(s)
    if len(words) < 2 or any(w in cfg['_stop'] for w in words):
        return None
    return s.rstrip(',')


def page_passengers(cfg: dict, text: str, fields: list) -> dict:
    """-> {names, empty (boxes without a name), count (passengers on the page, None = unknown), readable}."""
    names, signals, empty = [], [], 0
    # the name boxes on the form (their typed values; the page text does not include them)
    groups, cur = [], None
    for f in fields or []:
        k = field_kind(f)
        if not k:
            continue
        if cur is None or k in cur or (k == 'full') or ('full' in cur):
            cur = {}
            groups.append(cur)
        cur[k] = ' '.join(str(f.get('value') or '').split())
    for g in groups:
        full = g.get('full') or ' '.join(x for x in (g.get('first'), g.get('middle'), g.get('last')) if x)
        if ('full' in g and not g['full']) or ('full' not in g and not (g.get('first') and g.get('last'))):
            empty += 1
        elif full:
            names.append(full)
    if groups:
        signals.append(len(groups))
    # names listed under a passenger label in the page text
    lines = [ln.strip() for ln in str(text or '').split('\n')]
    for i, ln in enumerate(lines):
        m = cfg['_pax_label'].match(ln)
        if not m:
            continue
        rest = (m.group(m.lastindex) if m.lastindex else '') or ''
        got = []
        n = _is_name_line(cfg, rest)
        if n:
            got.append(n)
        j = i + 1
        while j < len(lines) and len(got) < 9:
            if not lines[j] or cfg['_pax_skip'].match(lines[j]):
                j += 1
                continue
            n = _is_name_line(cfg, lines[j])
            if not n:
                break
            got.append(n)
            j += 1
        for n in got:
            if not any(same_person(n, x) for x in names):
                names.append(n)
    # how many passengers the page says
    for rx in cfg['_pax_counts']:
        for m in rx.finditer(text or ''):
            try:
                signals.append(int(m.group(1)))
            except (TypeError, ValueError):
                pass
    if names:
        signals.append(len(names))
    count = max(signals) if signals else None
    return {'names': names, 'empty': empty, 'count': count, 'readable': bool(names or groups or signals)}


# ---- the page ---------------------------------------------------------------------------------------------------------

def flight_numbers(cfg: dict, text: str, ask: bool = False) -> list:
    """-> [(position, digits)] in text order, each flight number once ('WN 1234', 'Flight # 1234', '# 1234 / 5678')."""
    rx = cfg['_ask_flight_rx'] if ask else cfg['_flight_rx']
    out, seen = [], set()
    for m in rx.finditer(text or ''):
        anchor = (text or '')[m.start():m.start(1)]
        if anchor.strip().startswith('#') and cfg['_hash_skip'].search((text or '')[max(0, m.start() - 30):m.start()]):
            continue                                    # "Passenger # 1", "Seat # 12": not a flight
        for n in [m.group(1)] + re.findall(r'\d{1,4}', m.group(2) or ''):
            k = str(int(n))
            if k != '0' and k not in seen:
                seen.add(k)
                out.append((m.start(), k))
    return out


def read_page(cfg: dict, text: str, fields: list | None = None, ref: _dt.date | None = None) -> dict:
    ref = ref or today()
    text = str(text or '')
    flights = flight_numbers(cfg, text)
    dates = [d for d in parse_dates(text, ref) if not d['conflict']]
    times = parse_times(text)
    segs = []
    for pos, num in flights:
        before = [d for d in dates if d['end'] <= pos and d['start'] >= pos - 800]
        after = [d for d in dates if pos <= d['start'] <= pos + 300]
        d = before[-1] if before else (after[0] if after else None)
        lo = max(d['start'] if (d and d['start'] < pos) else pos - 400, pos - 400)
        near = [t for t in times if lo <= t['start'] <= pos + 250]
        segs.append({'flight': num, 'date': d['date'].isoformat() if d else None, 'time': near[0]['hm'] if near else None})
    trip_times = sorted({t['hm'] for pos, _ in flights for t in times if pos - 400 <= t['start'] <= pos + 400})
    known = cfg['airports']
    codes = []
    for m in CODE.finditer(text):
        c = m.group(1)
        if c in known and c not in codes:
            codes.append(c)
    fares = [name for name, rx, _ in cfg['_fares'] if rx.search(text)]
    refund = next((say for rx, say in cfg['_refund'] if rx.search(text)), None)
    return {'segments': segs, 'flights': [s['flight'] for s in segs],
            'dates': sorted({s['date'] for s in segs if s['date']}), 'times': trip_times, 'airports': codes,
            'passengers': page_passengers(cfg, text, fields or []), 'fares': fares, 'refund': refund,
            '_text': text, '_plain': _plain(text).lower()}


def summary(page: dict) -> dict:
    """What the page showed, small enough for buy-log.jsonl (no page text)."""
    p = page.get('passengers') or {}
    return {'flights': page.get('flights'), 'dates': page.get('dates'), 'times': page.get('times'),
            'airports': (page.get('airports') or [])[:6], 'passengers': p.get('names'), 'passenger_count': p.get('count'),
            'empty_name_boxes': p.get('empty'), 'fares': page.get('fares'), 'refund': page.get('refund')}


def airport_on_page(cfg: dict, page: dict, code: str) -> bool:
    """The code itself must be on the page whenever the page shows any airport code; the city name counts only on a
    page that shows no airport codes at all."""
    code = str(code or '').upper()
    if re.search(rf'(?<![A-Za-z]){re.escape(code)}(?![A-Za-z])', page.get('_text') or ''):
        return True
    if page.get('airports'):
        return False
    name = city(cfg, code)
    return bool(name) and bool(re.search(rf'(?<![a-z]){re.escape(_plain(name).lower())}(?![a-z])', page.get('_plain') or ''))


def fare_in(cfg: dict, fare: str, text: str, ask: bool = False) -> bool:
    key = fare_name(cfg, fare)
    for name, rx, rxi in cfg['_fares']:
        if name == key:
            return bool((rxi if ask else rx).search(text or ''))
    words = r'\W+'.join(re.escape(w) for w in re.findall(r'[A-Za-z0-9]+', str(fare)))
    return bool(words) and bool(re.search(rf'(?<![A-Za-z]){words}(?![A-Za-z])', text or '', re.I))


def fare_name(cfg: dict, fare: str | None) -> str | None:
    """The fare as the store names it ('basic+' -> 'Basic Plus'); unknown names are kept as given."""
    if not fare:
        return None
    key = lambda s: ' '.join(re.findall(r'[a-z0-9]+', str(s).lower().replace('+', ' plus ')))
    for name, _, _ in cfg['_fares']:
        if key(name) == key(fare):
            return name
    return ' '.join(str(fare).split())


def compare(cfg: dict, want: dict, page: dict) -> dict:
    """The owner's recorded trip (`want`, the grant's booking) against the page. -> {'unreadable', 'diffs' (plain words),
    'fields' (which details differ)}. Every detail is checked; nothing missing is ever treated as a match.
    Unreadable = the page shows no flight number and no flight date at all (not the final review page): asking the owner
    again would not help, Zeus has to reach the page that shows the trip."""
    pax = page.get('passengers') or {}
    if not page.get('flights') and not page.get('dates'):
        return {'unreadable': True, 'diffs': [], 'fields': []}
    diffs, fields = [], []
    fl = lambda xs: and_join(flight_label(cfg, x) for x in xs)
    # flights: the same set, nothing missing, nothing extra
    wf, pf = [str(int(x)) for x in want.get('flights') or []], list(page.get('flights') or [])
    if not pf:
        diffs.append('the page does not show a flight number')
        fields.append('flights')
    elif set(wf) != set(pf):
        diffs.append(f'the page shows flight {fl(pf)}, their yes is for {fl(wf)}')
        fields.append('flights')
    # dates: every flight on the page has a date, and the set of dates is the approved one
    wd, pd = sorted(set(want.get('dates') or [])), list(page.get('dates') or [])
    undated = [s['flight'] for s in page.get('segments') or [] if not s.get('date')]
    if not pd:
        diffs.append('the page does not show the flight date')
        fields.append('dates')
    else:
        if set(pd) != set(wd):
            diffs.append(f'the flight date on the page is {and_join(date_words(d) for d in pd)}, their yes is for '
                         f'{and_join(date_words(d) for d in wd)}')
            fields.append('dates')
        if undated:
            diffs.append(f'the page does not show the date of flight {fl(undated)}')
            fields.append('dates')
    # times the owner was told
    for hm in want.get('times') or []:
        if hm not in (page.get('times') or []):
            shown = and_join(time_words(t) for t in page.get('times') or []) or 'no flight times'
            diffs.append(f'the page does not show a {time_words(hm)} flight time (it shows {shown})')
            fields.append('times')
    # airports
    gone = [c for c in (want.get('from'), want.get('to')) if c and not airport_on_page(cfg, page, c)]
    if gone:
        shown = ', '.join(page.get('airports') or []) or 'no airport codes'
        diffs.append(f'the page does not show {and_join(_code_words(cfg, c) for c in gone)} (it shows {shown})')
        fields.append('airports')
    # passengers: each approved name, nobody else, and the same number of people
    wp = list(want.get('passengers') or [])
    names = list(pax.get('names') or [])
    if not pax.get('readable'):
        diffs.append('the page does not show the passenger names')
        fields.append('passengers')
    else:
        if pax.get('empty'):
            diffs.append(f"{pax['empty']} passenger name {'box is' if pax['empty'] == 1 else 'boxes are'} not filled in on the page")
            fields.append('passengers')
        missing = [w for w in wp if not any(same_person(w, n) for n in names) and not name_in_text(w, page.get('_plain') or '')]
        extra = [n for n in names if not any(same_person(w, n) for w in wp)]
        if missing:
            diffs.append(f'the page does not show {and_join(missing)}')
            fields.append('passengers')
        if extra:
            diffs.append(f"the page has {and_join(extra)}, who {'is' if len(extra) == 1 else 'are'} not in their yes")
            fields.append('passengers')
        count = pax.get('count')
        if count is None:
            diffs.append('the page does not show how many passengers there are')
            fields.append('passengers')
        elif count != len(wp) and not extra:
            diffs.append(f"the page has {count} passenger{'s' if count != 1 else ''}, their yes is for {len(wp)} ({and_join(wp)})")
            fields.append('passengers')
    # fare, when the owner was told one
    if want.get('fare') and not fare_in(cfg, want['fare'], page.get('_text') or ''):
        shown = and_join(page.get('fares') or []) or 'not shown'
        diffs.append(f"the fare on the page is {shown}, their yes is for {want['fare']}")
        fields.append('fare')
    return {'unreadable': False, 'diffs': diffs, 'fields': sorted(set(fields))}


def _code_words(cfg: dict, code: str) -> str:
    c = city(cfg, code)
    return f'{code} ({c})' if c else str(code)


# ---- Zeus's ask -------------------------------------------------------------------------------------------------------

def missing_in_ask(cfg: dict, want: dict, ask: str, ref: _dt.date | None = None) -> list:
    """The trip details the yes would bind that the ask it answered did not name. [] = all named."""
    ref = ref or today()
    ask = str(ask or '')
    out = []
    named = {d['date'].isoformat() for d in parse_dates(ask, ref) if not d['conflict']}
    out += [f'the date {date_words(d, ref)}' for d in want.get('dates') or [] if d not in named]
    plain = _plain(ask).lower()
    out += [str(p) for p in want.get('passengers') or [] if not name_in_text(p, plain)]
    said = {n for _, n in flight_numbers(cfg, ask, ask=True)}
    out += [f'flight {flight_label(cfg, f)}' for f in want.get('flights') or [] if str(int(f)) not in said]
    for code in (want.get('from'), want.get('to')):
        if not code:
            continue
        c = city(cfg, code)
        if not (re.search(rf'(?<![A-Za-z]){re.escape(code)}(?![A-Za-z])', ask, re.I)
                or (c and re.search(rf'(?<![a-z]){re.escape(_plain(c).lower())}(?![a-z])', plain))):
            out.append(_code_words(cfg, code))
    told = {t['hm'] for t in parse_times(ask)}
    out += [f'the time {time_words(t)}' for t in want.get('times') or [] if t not in told]
    if want.get('fare') and not fare_in(cfg, want['fare'], ask, ask=True):
        out.append(f"the fare ({want['fare']})")
    return out


# ---- what zeus-buy is given -------------------------------------------------------------------------------------------

def _split(values, rx=r'\s*[,;]\s*|\s+and\s+') -> list:
    out = []
    for v in values or []:
        out += [x.strip() for x in re.split(rx, str(v)) if x and x.strip()]
    return out


def validate(cfg: dict, opts: dict, ref: _dt.date | None = None) -> tuple:
    """opts: {'date': [...], 'flights': [...], 'from': [...], 'to': [...], 'passengers': [...], 'time': [...],
    'fare': [...]} (lists of raw strings). -> (booking dict, []) or (None, [plain problems])."""
    ref = ref or today()
    problems, need = [], []
    for flag in ('date', 'flights', 'from', 'to', 'passengers'):
        if not opts.get(flag):
            need.append('--' + flag)
    if need:
        return None, ['missing ' + and_join(need)]
    dates = []
    for raw in _split(opts['date'], r'\s*[,;]\s*|\s+and\s+|\s+'):
        found = parse_dates(raw, ref)
        if len(found) != 1 or found[0]['start'] != 0 or found[0]['end'] != len(raw) or found[0]['conflict'] or \
                not re.search(r'\d{4}', raw):
            problems.append(f'"{raw}" is not a date like YYYY-MM-DD')
            continue
        d = found[0]['date']
        if d < ref:
            problems.append(f'{date_words(d.isoformat(), ref)} is in the past')
        elif d > ref + _dt.timedelta(days=400):
            problems.append(f'{date_words(d.isoformat(), ref)} is more than a year away')
        else:
            dates.append(d.isoformat())
    flights = []
    for raw in _split(opts['flights'], r'\s*[,;/]\s*|\s+and\s+'):
        m = re.fullmatch(r'(?i:([A-Z]{2})?\s?(?:flight\s*)?#?\s*)(\d{1,4})', raw.strip())
        code = cfg.get('airline_code') or ''
        if not m or int(m.group(2)) == 0 or (m.group(1) and m.group(1).upper() != code.upper()):
            problems.append(f'"{raw}" is not a flight number like {flight_label(cfg, "1234")}')
        elif str(int(m.group(2))) not in flights:
            flights.append(str(int(m.group(2))))
    ends = {}
    for flag in ('from', 'to'):
        vals = _split(opts[flag])
        if len(vals) != 1 or not re.fullmatch(r'[A-Za-z]{3}', vals[0]):
            problems.append(f'--{flag} needs one 3-letter airport code like SFO (got "{" ".join(opts[flag])}")')
        else:
            ends[flag] = vals[0].upper()
    if ends.get('from') and ends.get('from') == ends.get('to'):
        problems.append('--from and --to are the same airport')
    people = []
    for raw in _split(opts['passengers']):
        name = ' '.join(raw.split())
        if len(name_tokens(name)) < 2 or re.search(r'[^A-Za-zÀ-ɏ\' .\-’]', name):
            problems.append(f'"{name}" is not a full name (first and last) as it is on the booking')
        elif not any(same_person(name, p) for p in people):
            people.append(name)
    if len(people) > 9:
        problems.append('more than 9 passengers')
    times = []
    for raw in _split(opts.get('time') or []):
        t = parse_times(raw)
        if len(t) != 1:
            problems.append(f'"{raw}" is not a time like 5:30am')
        elif t[0]['hm'] not in times:
            times.append(t[0]['hm'])
    fare = None
    if opts.get('fare'):
        fare = fare_name(cfg, ' '.join(' '.join(opts['fare']).split()))
        if not fare or len(fare) > 40:
            problems.append('--fare needs the fare name as the page shows it, like "Basic"')
    if problems:
        return None, problems
    return {'kind': 'flight', 'dates': sorted(set(dates)), 'flights': flights, 'from': ends['from'], 'to': ends['to'],
            'passengers': people, 'times': times, 'fare': fare}, []


# ---- plain texts ------------------------------------------------------------------------------------------------------

def from_page(page: dict) -> dict:
    """The trip as the page shows it (best effort; parts it cannot read are None), to fill in the example ask."""
    segs = page.get('segments') or []
    codes = page.get('airports') or []
    fares = page.get('fares') or []
    return {'kind': 'flight', 'dates': page.get('dates') or [], 'flights': page.get('flights') or [],
            'from': codes[0] if codes else None, 'to': codes[-1] if len(codes) > 1 else None,
            'passengers': (page.get('passengers') or {}).get('names') or [],
            'times': [s['time'] for s in segs if s.get('time')], 'fare': fares[0] if len(fares) == 1 else None,
            '_segments': segs, '_refund': page.get('refund')}


def merged(page_trip: dict | None, want: dict | None) -> dict:
    """The page's trip, with the grant's details filling what the page did not show."""
    out = dict(want or {})
    for k, v in (page_trip or {}).items():
        if v:
            out[k] = v
    return out


def _legs(cfg: dict, b: dict, ref) -> str:
    segs = [s for s in b.get('_segments') or [] if s.get('flight')]
    if segs and all(s.get('date') for s in segs):
        parts = []
        for s in segs:
            when = date_words(s['date'], ref) + (' ' + time_words(s['time']) if s.get('time') else '')
            parts.append(f"{when}, {flight_label(cfg, s['flight'])}")
        return '; then '.join(parts)
    dates = [date_words(d, ref) for d in b.get('dates') or []] or ['<date>']
    times = [time_words(t) for t in b.get('times') or []]
    flights = [flight_label(cfg, f) for f in b.get('flights') or []] or ['<flight>']
    if len(dates) == len(flights) and (not times or len(times) == len(flights)):
        return '; then '.join(f"{d}{' ' + times[i] if times else ''}, {flights[i]}" for i, d in enumerate(dates))
    return f"{and_join(dates)}{' ' + and_join(times) if times else ''}, {and_join(flights)}"


def route(cfg: dict, b: dict) -> str:
    to = b.get('to')
    return f"{b.get('from') or '<from>'} to {city(cfg, to) or to or '<to>'}"


def ask_line(cfg: dict, store_name: str, b: dict, total_cents: int | None, refund: str | None = None,
             ref: _dt.date | None = None) -> str:
    """'Example Air: SFO to Denver, Sat Mar 2 5:30am, XX 1234, for Alex Rivera, $243.96, Basic
    (refundable as flight credit). Book it?'"""
    ref = ref or today()
    total = f'${total_cents // 100:,}.{total_cents % 100:02d}' if total_cents is not None else '$<total>'
    who = and_join(b.get('passengers') or []) or '<passenger names>'
    refund = refund or b.get('_refund')
    tail = ''
    if b.get('fare'):
        tail = f", {b['fare']}" + (f' ({refund})' if refund else '')
    elif refund:
        tail = f', {refund}'
    return f"{store_name}: {route(cfg, b)}, {_legs(cfg, b, ref)}, for {who}, {total}{tail}. {cfg.get('ask_verb') or 'Book it?'}"


def describe(cfg: dict, b: dict, ref: _dt.date | None = None) -> str:
    """'SFO to Denver, Sat Mar 2 5:30am, XX 1234, for Alex Rivera, Basic'."""
    ref = ref or today()
    who = and_join(b.get('passengers') or []) or 'unknown passengers'
    return f"{route(cfg, b)}, {_legs(cfg, b, ref)}, for {who}" + (f", {b['fare']}" if b.get('fare') else '')


def what(cfg: dict, b: dict) -> str:
    return f"flight {b.get('from') or '<from>'} to {b.get('to') or '<to>'}"


def cmd_flags(cfg: dict, b: dict) -> str:
    q = lambda s: f'"{s}"' if re.search(r'[\s,]', s) else s
    parts = [f"--date {','.join(b.get('dates') or []) or '<YYYY-MM-DD>'}",
             f"--flights {q(', '.join(flight_label(cfg, f) for f in b.get('flights') or [])) if b.get('flights') else '<flight number>'}",
             f"--from {b.get('from') or '<airport code>'}", f"--to {b.get('to') or '<airport code>'}",
             f"--passengers {q(', '.join(b.get('passengers'))) if b.get('passengers') else chr(34) + '<full names>' + chr(34)}"]
    if b.get('times'):
        parts.append(f"--time {q(', '.join(time_words(t) for t in b['times']))}")
    if b.get('fare'):
        parts.append(f'--fare {q(b["fare"])}')
    return ' '.join(parts)
