"""travel: notice a trip on the owner's calendar and ask them once whether Zeus's clock should follow them.

The clock itself lives in ownertime.py, which moves quiet hours to where the owner is; without this module only
`life zone` with the owner's own words naming the place can set it (lifezone.py), so a trip on the calendar would change
nothing unless the owner happened to say where they were.

What this does, all in code (no model decides anything here):
  detect   reads the calendar snapshot (`calendar.snapshot_file`), only the owner's own calendars (`calendar.own_calendars`;
           shared and family calendars are left out). A trip counts only when it is confirmed: an outbound flight and a
           flight back, a flight and a hotel or stay there, or an event of two days or more whose place is known. Its place
           must sit on ONE clock (lifezone.resolve, plus a short table of airport codes); a name on two clocks
           (Portland, Tennessee) is never guessed. Cancelled items, other people's travel ("Mom and Grandpa depart",
           "Taylor arrives"), trips over lifezone.MAX_DAYS, and places on the same clock as home on every day of the
           trip are left out. Travel is never inferred from the hours the owner replies at.
  ask      once per trip: an ordinary life item, put on the owner's list from 9:00am the day before they leave, only
           between 9am and 9pm their clock, worded by the note writer like every other item ("You're in Nashville Tuesday
           to Friday, want me on Central time for that?"). The trip is marked asked BEFORE the item exists: a crash in
           between means never asked, not asked twice. Same place and overlapping days = the same trip.
  yes      `life approve <item> "<the owner's words>"` (lifecli.py calls approve here). Code finds the owner's real
           message since they were asked, reads it alone (hisreply.py: a clear yes plus filler is a yes; "sounds good"
           only right after this ask; "ok" is not), checks that the text they answered names the place, then sets the
           clock through ownertime.set_zone from the trip's first day through its last day, keeping their words and the
           ask they answered. No zone is ever set without the owner's yes (lifezone.NEEDS_HIS_WORDS stays True).
  no / none  a no, or no answer before they leave, changes nothing, and that trip is never asked about again.

Switch: the file `travel-ask-on` in the life folder, and it also needs `zone-on` (the clock itself). Without either,
nothing here runs. No calendar configured = no trips. State: <life>/travel-asks.json (one line per trip seen).
Nothing here sends anything; nothing here writes owner-zone.json except approve, on the owner's yes.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ownertime as OT
import settings

SWITCH = 'travel-ask-on'
STATE = 'travel-asks.json'
LIVE_LIFE = Path(OT.LIVE_LIFE)
_CAL_FILE = settings.get('calendar.snapshot_file')
CAL_SNAPSHOT = Path(_CAL_FILE).expanduser() if _CAL_FILE else None   # the file sources.CAL_SNAPSHOT names; None = no calendar
SNAPSHOT_FRESH_H = 6          # an older or incomplete copy starts no new ask and closes nothing as "gone"
ASK_HOUR, LAST_HOUR = 9, 21   # the owner's clock
JUST_ASKED = 15 * 60          # "sounds good" answers this ask only this soon after it (hisreply / onesend)
CONNECTION_H = 6              # a flight that leaves within this long of landing is the next leg of the same journey
STAY_NEAR_DAYS = 1            # a hotel counts with a flight when it starts within a day of landing
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
# The owner's own calendars (calendar ids as the snapshot names them). Default: the owner's mailbox addresses. Empty =
# every calendar in the snapshot counts as the owner's.
OWN_CALENDARS = frozenset(str(c).lower() for c in (settings.get('calendar.own_calendars') or list(settings.OWNER_EMAILS.values())) if c)

_P, _M, _C, _E = 'America/Los_Angeles', 'America/Denver', 'America/Chicago', 'America/New_York'
# Airport codes are facts, not guesses: each is one airport on one clock.
AIRPORTS = {
    'ONT': ('Ontario', _P), 'LAX': ('Los Angeles', _P), 'BUR': ('Burbank', _P), 'SNA': ('Orange County', _P), 'LGB': ('Long Beach', _P),
    'PSP': ('Palm Springs', _P), 'SAN': ('San Diego', _P), 'SFO': ('San Francisco', _P), 'OAK': ('Oakland', _P), 'SJC': ('San Jose', _P),
    'SMF': ('Sacramento', _P), 'SEA': ('Seattle', _P), 'PDX': ('Portland, Oregon', _P), 'LAS': ('Las Vegas', _P), 'RNO': ('Reno', _P),
    'GEG': ('Spokane', _P), 'PHX': ('Phoenix', 'America/Phoenix'), 'TUS': ('Tucson', 'America/Phoenix'), 'SLC': ('Salt Lake City', _M),
    'DEN': ('Denver', _M), 'BOI': ('Boise', 'America/Boise'), 'ABQ': ('Albuquerque', _M), 'ELP': ('El Paso', _M), 'DFW': ('Dallas', _C),
    'DAL': ('Dallas', _C), 'IAH': ('Houston', _C), 'HOU': ('Houston', _C), 'AUS': ('Austin', _C), 'SAT': ('San Antonio', _C),
    'MSY': ('New Orleans', _C), 'OKC': ('Oklahoma City', _C), 'TUL': ('Tulsa', _C), 'MCI': ('Kansas City', _C), 'STL': ('St Louis', _C),
    'MSP': ('Minneapolis', _C), 'MKE': ('Milwaukee', _C), 'ORD': ('Chicago', _C), 'MDW': ('Chicago', _C), 'BNA': ('Nashville', _C),
    'MEM': ('Memphis', _C), 'BHM': ('Birmingham, Alabama', _C), 'OMA': ('Omaha', _C), 'ATL': ('Atlanta', _E), 'CLT': ('Charlotte', _E),
    'RDU': ('Raleigh', _E), 'MIA': ('Miami', _E), 'FLL': ('Fort Lauderdale', _E), 'MCO': ('Orlando', _E), 'TPA': ('Tampa', _E),
    'JAX': ('Jacksonville', _E), 'PBI': ('West Palm Beach', _E), 'RSW': ('Fort Myers', _E), 'BOS': ('Boston', _E), 'JFK': ('New York', _E),
    'LGA': ('New York', _E), 'EWR': ('Newark', _E), 'PHL': ('Philadelphia', _E), 'DCA': ('Washington, DC', _E), 'IAD': ('Washington, DC', _E),
    'BWI': ('Baltimore', _E), 'DTW': ('Detroit', _E), 'CLE': ('Cleveland', _E), 'CMH': ('Columbus', _E), 'CVG': ('Cincinnati', _E),
    'PIT': ('Pittsburgh', _E), 'IND': ('Indianapolis', 'America/Indiana/Indianapolis'), 'SDF': ('Louisville', 'America/Kentucky/Louisville'),
    'TYS': ('Knoxville', _E), 'CHS': ('Charleston', _E), 'SAV': ('Savannah', _E), 'HNL': ('Honolulu', 'Pacific/Honolulu'),
    'OGG': ('Maui', 'Pacific/Honolulu'), 'KOA': ('Kona', 'Pacific/Honolulu'), 'LIH': ('Kauai', 'Pacific/Honolulu'),
    'ANC': ('Anchorage', 'America/Anchorage'), 'MEX': ('Mexico City', 'America/Mexico_City'), 'CUN': ('Cancun', 'America/Cancun'),
    'SJD': ('Cabo', 'America/Mazatlan'), 'PVR': ('Puerto Vallarta', 'America/Mexico_City'), 'YYZ': ('Toronto', 'America/Toronto'),
    'YVR': ('Vancouver', 'America/Vancouver'), 'LHR': ('London', 'Europe/London'), 'CDG': ('Paris', 'Europe/Paris'),
    'NRT': ('Tokyo', 'Asia/Tokyo'), 'HND': ('Tokyo', 'Asia/Tokyo'),
}
STATE_ABBR = {'al': 'alabama', 'ak': 'alaska', 'az': 'arizona', 'ar': 'arkansas', 'ca': 'california', 'co': 'colorado', 'ct': 'connecticut',
              'de': 'delaware', 'fl': 'florida', 'ga': 'georgia', 'hi': 'hawaii', 'id': 'idaho', 'il': 'illinois', 'in': 'indiana', 'ia': 'iowa',
              'ks': 'kansas', 'ky': 'kentucky', 'la': 'louisiana', 'me': 'maine', 'md': 'maryland', 'ma': 'massachusetts', 'mi': 'michigan',
              'mn': 'minnesota', 'ms': 'mississippi', 'mo': 'missouri', 'mt': 'montana', 'ne': 'nebraska', 'nv': 'nevada',
              'nh': 'new hampshire', 'nj': 'new jersey', 'nm': 'new mexico', 'ny': 'new york state', 'nc': 'north carolina',
              'nd': 'north dakota', 'oh': 'ohio', 'ok': 'oklahoma', 'or': 'oregon', 'pa': 'pennsylvania', 'ri': 'rhode island',
              'sc': 'south carolina', 'sd': 'south dakota', 'tn': 'tennessee', 'tx': 'texas', 'ut': 'utah', 'vt': 'vermont', 'va': 'virginia',
              'wa': 'washington state', 'wv': 'west virginia', 'wi': 'wisconsin', 'wy': 'wyoming', 'dc': 'washington dc'}
FLIGHTISH = re.compile(r'\bflight\b|\b(?:southwest|delta|united|american airlines|alaska airlines|jetblue|spirit|frontier|hawaiian|aeromexico|volaris)\b'
                       r'|\b(?:WN|AA|DL|UA|AS|B6|NK|F9|HA|AM|Y4|VB)\s?\d{1,4}\b', re.I)
CODES = re.compile(r'\b([A-Z]{3})\s*(?:to|→|->|–|—|-|>)\s*([A-Z]{3})\b')
TO_PLACE = re.compile(r'\bflight to\s+(.+?)\s*(?:\(|$)', re.I)
STAY = re.compile(r'\b(?:stay(?:ing)? (?:at|in)|hotel|inn|resort|airbnb|vrbo|lodge|suites|marriott|hilton|hyatt|sheraton|westin|hampton|check-?in)\b', re.I)
TRIP_TITLE = re.compile(r'^(?:trip to|travel to|trip:|travel:|in|visit(?:ing)?|vacation in|vacation to|stay(?:ing)? in)\s+(.+)$'
                        r'|^(.+?)\s+(?:trip|vacation|getaway|weekend)$', re.I)
OTHERS = re.compile(r"\b(?:mom|mother|father|parents?|grandma|grandpa|grandparents?|wife|husband|kids?|boys|girls|son|daughter|guests?|"
                    r"visitors?|in-?laws?|brother|sister|aunt|uncle|cousins?|nanny)\b|\barriv(?:es|ing|al)\b|\bpick(?:ing)? ?up\b|\b[A-Z][a-z]+['’]s\b", re.I)
CANCELLED = re.compile(r'\bcancell?ed\b|^\W*cancel', re.I)
NOT_FOR_OWNER_EYES = re.compile(r'[`{}\[\]<>_]')


def on(life) -> bool:
    """Both switch files: this one, and zone-on (without the clock there is nothing to ask about)."""
    return (Path(life) / SWITCH).exists() and (Path(life) / OT.ZONE_SWITCH).exists()


def is_travel(p: dict) -> bool:
    return isinstance(p.get('travel'), dict) and bool(p['travel'].get('trip'))


# ---- reading the calendar ----------------------------------------------------------------------------------------------

def _instant(value):
    t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo(OT.HOME))


def _all_day_date(value) -> date:
    """An all-day start or end is midnight on its calendar's own clock, written in UTC: the nearest UTC date is the day."""
    return (_instant(value).astimezone(timezone.utc) + timedelta(hours=12)).date()


def _his(e: dict) -> bool:
    """Is the event on one of the owner's own calendars?"""
    if not OWN_CALENDARS:
        return True
    ids = {str(x).lower() for x in (e.get('calendar_ids') or [])} | {str(x).lower() for x in (e.get('calendars') or [])}
    return bool(ids & OWN_CALENDARS)


def _left_out(e: dict) -> str:
    """Why an event on the owner's calendar is not their own travel, or ''."""
    title = str(e.get('title') or '')
    if str(e.get('status') or '').lower() == 'cancelled' or CANCELLED.search(title):
        return 'cancelled'
    if OTHERS.search(title):
        return 'about someone else'
    return ''


def _resolved(words):
    """-> (zone key, place as the owner would say it) or None, never a guess between two clocks."""
    import lifezone as Z
    try:
        found = Z.resolve(words)
    except Exception:
        return None
    return (found[1], found[2]) if found and found[0] == 'zone' and found[1] else None


def place_from_location(loc):
    """'Bridgestone Arena, 501 Broadway, Nashville, TN 37203, USA' -> ('America/Chicago', 'Nashville'). A city with its
    state or country must resolve as that pair; when it does not, nothing is guessed from the city alone."""
    import lifezone as Z
    keep = []
    for part in str(loc or '').split(','):
        f = re.sub(r'\b\d{5}(?:-\d{4})?\b', ' ', Z._flat(part)).strip()
        if not f or f[0].isdigit() or f in ('usa', 'us', 'united states', 'united states of america'):
            continue
        keep.append(STATE_ABBR.get(f, f))
    if not keep:
        return None
    if len(keep) >= 2:
        got = _resolved(keep[-2] + ' ' + keep[-1])
        if got:
            return got[0], ' '.join(w.capitalize() for w in keep[-2].split())
        return None
    return _resolved(keep[0])


def _flight(e: dict):
    """-> {'start', 'end', 'dest_zone', 'dest', 'title'} for a flight whose landing place is known, else None."""
    title = str(e.get('title') or '')
    codes = CODES.search(title)
    to = TO_PLACE.search(title)
    if not (FLIGHTISH.search(title) or (codes and codes.group(1) in AIRPORTS and codes.group(2) in AIRPORTS)):
        return None
    by_code = AIRPORTS.get(codes.group(2)) if codes else None
    by_name = _resolved(to.group(1)) if to else None
    if by_code and by_name and _offsets(by_code[1]) != _offsets(by_name[0]):
        return None                                   # the code and the name disagree: not known
    dest = (by_name[0], by_name[1]) if by_name else ((by_code[1], by_code[0]) if by_code else None)
    if not dest:
        return None
    try:
        return {'start': _instant(e['start']), 'end': _instant(e.get('end') or e['start']), 'dest_zone': dest[0], 'dest': dest[1], 'title': title}
    except (KeyError, TypeError, ValueError):
        return None


def _offsets(zone_key: str) -> tuple:
    year = datetime.fromtimestamp(OT._clock()).year
    z = ZoneInfo(zone_key)
    return tuple(datetime(year, m, 15, 12, tzinfo=z).utcoffset() for m in (1, 7))


def _days(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def same_clock(zone_a: str, zone_b: str, first: date, last: date) -> bool:
    """The two zones show the same time on every day of the trip (Las Vegas and home: yes; Phoenix in winter: no)."""
    a, b = ZoneInfo(zone_a), ZoneInfo(zone_b)
    return all(datetime(d.year, d.month, d.day, 12, tzinfo=a).utcoffset() == datetime(d.year, d.month, d.day, 12, tzinfo=b).utcoffset()
               for d in _days(first, last))


def _span(e: dict, zone_key: str):
    """(first day, last day) of an event, on the clock of the place it is in."""
    if e.get('all_day'):
        first, last = _all_day_date(e['start']), _all_day_date(e.get('end') or e['start']) - timedelta(days=1)
    else:
        z = ZoneInfo(zone_key)
        first, last = _instant(e['start']).astimezone(z).date(), _instant(e.get('end') or e['start']).astimezone(z).date()
    return first, max(first, last)


def _stay(e: dict):
    """-> {'zone', 'place', 'first', 'last', 'title', 'hotel'} for a stay or an event of two days or more whose place is known."""
    title = str(e.get('title') or '')
    got = place_from_location(e.get('location')) if e.get('location') else None
    if not got:
        m = TRIP_TITLE.match(title.strip())
        words = (m.group(1) or m.group(2)) if m else (title if len(title.split()) <= 3 and not re.search(r'\d', title) else '')
        got = _resolved(words) if words else None
    if not got:
        return None
    try:
        first, last = _span(e, got[0])
    except (KeyError, TypeError, ValueError):
        return None
    hotel = bool(STAY.search(title))
    if last <= first and not hotel:
        return None                                   # one day is an outing, not a trip
    return {'zone': got[0], 'place': got[1], 'first': first, 'last': max(first, last), 'title': title, 'hotel': hotel}


def detect(events: list, now: float | None = None, life=None, home: str = OT.HOME) -> tuple:
    """-> (trips, left_out). Pure: reads only the events given. Each trip: {'trip': id, 'zone', 'place', 'first',
    'last', 'departure' (epoch seconds), 'how', 'evidence': [titles]}. left_out: [(title, why)] for the report."""
    import lifezone as Z
    now = OT._clock() if now is None else float(now)
    left, flights, stays = [], [], []
    seen = set()
    for e in events or []:
        if not isinstance(e, dict) or not _his(e):
            continue
        why = _left_out(e)
        if why:
            left.append((e.get('title'), why))
            continue
        f = _flight(e)
        if f:
            k = (f['start'].timestamp(), f['end'].timestamp())
            if k in seen:                                     # the same flight on two of the owner's calendars
                continue
            seen.add(k)
            flights.append(f)
            continue
        s = _stay(e)
        if s:
            stays.append(s)
    flights.sort(key=lambda f: f['start'])
    journeys = []                                     # connecting legs joined: where the owner really lands
    for f in flights:
        j = journeys[-1] if journeys else None
        if j and (f['start'] - j['end']).total_seconds() < CONNECTION_H * 3600:
            j.update(end=f['end'], dest_zone=f['dest_zone'], dest=f['dest'], title=j['title'] + '; ' + f['title'])
        else:
            journeys.append(dict(f))
    trips, home_legs = [], set()
    for i, f in enumerate(journeys):
        if i in home_legs:
            continue                                  # the flight back of a trip already read
        zone = f['dest_zone']
        first = f['end'].astimezone(ZoneInfo(zone)).date()
        back = next((g for g in journeys[i + 1:] if g['dest'] != f['dest'] or g['dest_zone'] != zone), None)
        last, how, ev = None, None, [f['title']]
        if back:
            last, how = back['start'].astimezone(ZoneInfo(zone)).date(), 'flights'
            ev.append(back['title'])
            home_legs.add(journeys.index(back))
        near = [s for s in stays if same_clock(s['zone'], zone, s['first'], s['first']) and abs((s['first'] - first).days) <= STAY_NEAR_DAYS]
        if near:
            if last is None:
                last, how = max(s['last'] for s in near), 'flight and stay'
            ev += [s['title'] for s in near]
            for s in near:
                s['used'] = True
        if last is None:
            if not same_clock(zone, home, first, first):
                left.append((f['title'], 'one-way: no flight back and no stay there'))
            continue
        trips.append({'zone': zone, 'place': f['dest'], 'first': first, 'last': max(first, last), 'departure': f['start'].timestamp(),
                      'how': how, 'evidence': ev})
    for s in stays:
        if s.get('used'):
            continue
        dep = datetime(s['first'].year, s['first'].month, s['first'].day, tzinfo=ZoneInfo(s['zone'])).timestamp()
        trips.append({'zone': s['zone'], 'place': s['place'], 'first': s['first'], 'last': s['last'], 'departure': dep, 'how': 'event',
                      'evidence': [s['title']]})
    merged = []
    for t in sorted(trips, key=lambda t: t['first']):
        m = next((x for x in merged if same_clock(x['zone'], t['zone'], t['first'], t['first']) and t['first'] <= x['last'] + timedelta(days=1)), None)
        if m:
            m['last'] = max(m['last'], t['last'])
            m['departure'] = min(m['departure'], t['departure'])
            m['evidence'] += [x for x in t['evidence'] if x not in m['evidence']]
            continue
        merged.append(dict(t))
    rule = OT.rule(life) if life else None
    out = []
    for t in merged:
        today = datetime.fromtimestamp(now, ZoneInfo(t['zone'])).date()
        if same_clock(t['zone'], home, t['first'], t['last']):
            left.append((t['place'], 'same clock as home'))
            continue
        if t['last'] < today or t['departure'] <= now:
            left.append((t['place'], 'already under way or over'))
            continue
        if (t['last'] - t['first']).days > Z.MAX_DAYS:
            left.append((t['place'], f'longer than {Z.MAX_DAYS} days'))
            continue
        if rule and rule['zone'].key == t['zone'] and rule['until'] and rule['until'] >= t['last'] \
                and (rule.get('from') is None or rule['from'] <= t['first']):
            left.append((t['place'], 'the owner\'s clock is already set for it'))
            continue
        t['trip'] = f"{t['zone']}:{t['first'].isoformat()}"
        out.append(t)
    return out, left


def read_snapshot(path=None) -> tuple:
    """-> (events, ok, why). ok = complete and fresh enough to start an ask or call a trip gone."""
    if not path and CAL_SNAPSHOT is None:
        return [], False, 'no calendar is configured'
    p = Path(path or CAL_SNAPSHOT)
    try:
        snap = json.loads(p.read_text())
    except (OSError, ValueError):
        return [], False, 'the calendar copy could not be read'
    events = list(snap.get('events') or [])
    try:
        age = OT._clock() - _instant(snap.get('checked_at')).timestamp()
    except (TypeError, ValueError):
        age = None
    if not snap.get('complete'):
        return events, False, 'the calendar copy is incomplete'
    if age is None or age > SNAPSHOT_FRESH_H * 3600:
        return events, False, 'the calendar copy is old'
    return events, True, ''


# ---- words ---------------------------------------------------------------------------------------------------------------

def span_words(first: date, last: date) -> str:
    """'Tuesday to Friday', 'on Tuesday', or 'November 17 to November 28' for a longer stretch."""
    if first == last:
        return 'on ' + first.strftime('%A')
    if (last - first).days < 7:
        return f"{first.strftime('%A')} to {last.strftime('%A')}"
    return f"{first.strftime('%B')} {first.day} to {last.strftime('%B')} {last.day}"


def _clean(text) -> str:
    return ' '.join(NOT_FOR_OWNER_EYES.sub(' ', str(text or '')).split())


def ask_text(t: dict) -> str:
    """The question, as code words it. It starts in lower case on purpose: lastsaid and the notifier match capitalised
    words to tell which ask a message is about, and an opening "Want" or "You" would match every message."""
    label = OT.label(ZoneInfo(t['zone']))
    return f"in {t['place']} {span_words(t['first'], t['last'])}: want Zeus on {label} time for that? It is a plain yes or no."


def packet_for(t: dict) -> dict:
    import enddate as E
    label = OT.label(ZoneInfo(t['zone']))
    today = datetime.fromtimestamp(OT._clock(), ZoneInfo(t['zone'])).date()
    first, last = E.plain_day(t['first'], today), E.plain_day(t['last'], today)
    seen = '; '.join(_clean(x) for x in t['evidence'][:4])
    return {'status': 'needs_decision',
            'headline': f"{settings.OWNER_FIRST} is in {t['place']} {span_words(t['first'], t['last'])}. That is on {label} time.",
            'details': (f"What their own calendar shows: {seen}. If they say yes, their quiet hours follow {label} time "
                        f"from {first} through {last}, then go back to home time by themselves. If they say no, or do not answer before they "
                        'leave, nothing changes and this trip is not asked about again.'),
            'decision': {'ask': ask_text(t),
                         'ready': (f'On their yes, their quiet hours follow {label} time from {first} through {last}, then go '
                                   'back to home time by themselves. On their no, nothing changes and this trip is not asked about again.'),
                         'default': '', 'by': 'zeus'},
            'followups': [], 'learned': []}


# ---- the state file --------------------------------------------------------------------------------------------------

def _load(life) -> dict:
    try:
        d = json.loads((Path(life) / STATE).read_text())
        return d if isinstance(d, dict) and isinstance(d.get('trips'), dict) else {'trips': {}}
    except (OSError, ValueError):
        return {'trips': {}}


def _save(life, d: dict):
    tmp = Path(life) / (STATE + '.tmp')
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str))
    tmp.replace(Path(life) / STATE)


def _mark(life, trip_id: str, **fields):
    d = _load(life)
    d['trips'].setdefault(trip_id, {}).update(fields, at=time.time())
    _save(life, d)


def _same_trip(rec: dict, t: dict) -> bool:
    try:
        first, last = date.fromisoformat(rec['first']), date.fromisoformat(rec['last'])
    except (KeyError, TypeError, ValueError):
        return False
    return rec.get('zone') == t['zone'] and first <= t['last'] and t['first'] <= last


def _close(con, key: str, state: str, why: str, by: str = 'travel ask', extra=None):
    r = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
    p = json.loads(r[0])
    p['closed'] = {'state': state, 'at': time.time(), 'by': by, 'why': why} | (extra or {})
    con.execute('UPDATE opps SET state=?, payload=?, updated=? WHERE key=?', (state, json.dumps(p, default=str), time.time(), key))
    con.commit()


# ---- every tick ----------------------------------------------------------------------------------------------------------

def _slug(text) -> str:
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')[:40] or 'trip'


def make_ask(con, life, t: dict, now_dt: datetime) -> str | None:
    key = f"travel:{_slug(t['place'])}:{t['first'].isoformat()}"
    if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
        return None
    _mark(life, t['trip'], state='asked', item=key, zone=t['zone'], place=t['place'], first=t['first'].isoformat(),
          last=t['last'].isoformat(), departure=t['departure'])          # marked first: a crash means never asked, not asked twice
    now = time.time()
    step = f'Ask {settings.OWNER_FIRST} one yes-or-no question: should their clock follow them on this trip?'
    payload = {'title': f"{t['place']} trip: which clock"[:120], 'why': 'Their quiet hours could follow their clock while they are away.',
               'evidence': '; '.join(_clean(x) for x in t['evidence'][:4]), 'urgency': 'today', 'dedupe_key': key, 'confidence': 1.0,
               'owner_involved': 'yes', 'action_class': 'notify_owner_now', 'proposed_action': step,
               'steps': [{'do': step, 'action_class': 'notify_owner_now'}],
               'event_start': datetime.fromtimestamp(t['departure'], timezone.utc).isoformat(),
               'decision': {'autonomy': 'ask', 'surface_at': now_dt.isoformat(), 'channel': 'imessage', 'reason': 'travel clock, the day before they leave'},
               '_source': 'travel', 'packet': packet_for(t), 'ready_at': now,
               'travel': {'trip': t['trip'], 'zone': t['zone'], 'place': t['place'], 'first': t['first'].isoformat(), 'last': t['last'].isoformat(),
                          'departure': t['departure'], 'how': t['how'], 'evidence': t['evidence'], 'ask': ask_text(t)}}
    con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key, 'ready', 'ask', now_dt.isoformat(), 'today', 'notify_owner_now', json.dumps(payload, default=str), '[]', now, now))
    con.commit()
    return key


def tend(con, life, now_dt: datetime | None = None, snapshot=None) -> dict:
    """Once a tick (tick.run_phases, after offers). Closes asks that ran out or whose trip left the owner's calendar, then
    puts at most the due asks on their list. -> a small report, {} when nothing happened."""
    life = Path(life)
    if not on(life):
        return {}
    now_dt = now_dt or datetime.now(OT.OWNER)
    now = now_dt.timestamp()
    events, ok, why = read_snapshot(snapshot)
    trips, left = detect(events, now, life)
    out = {}
    state = _load(life)
    for r in con.execute("SELECT key,state,payload FROM opps WHERE key LIKE 'travel:%'").fetchall():
        key, st, p = r[0], r[1], json.loads(r[2])
        t = p.get('travel') or {}
        rec = state['trips'].get(t.get('trip')) or {}
        if st in OPEN and not p.get('closed'):
            if now >= float(t.get('departure') or 0):
                _close(con, key, 'moot', 'no answer before they left; this trip is not asked about again')
                _mark(life, t['trip'], state='unanswered')
                out.setdefault('closed', []).append(key)
            elif ok and not any(x['trip'] == t.get('trip') or _same_trip(t, x) for x in trips):
                _close(con, key, 'moot', 'the trip is no longer on the owner\'s calendar')
                _mark(life, t['trip'], state='gone')
                out.setdefault('closed', []).append(key)
        elif rec.get('state') == 'asked':
            _mark(life, t['trip'], state='no' if st == 'declined' else 'yes' if (p.get('closed') or {}).get('zone_set') else 'unanswered')
    if not ok:
        if trips:
            out['waiting'] = why
        return out
    state = _load(life)
    for t in trips:
        if any(_same_trip(rec, t) for rec in state['trips'].values()):
            continue
        dep_clock = OT.zone(at=t['departure'], life=life)
        dep_local = datetime.fromtimestamp(t['departure'], dep_clock)
        ask_from = datetime(dep_local.year, dep_local.month, dep_local.day, ASK_HOUR, tzinfo=dep_clock) - timedelta(days=1)
        here = datetime.fromtimestamp(now, OT.zone(at=now, life=life))
        if now < ask_from.timestamp() or not (ASK_HOUR <= here.hour < LAST_HOUR):
            continue
        key = make_ask(con, life, t, here)
        if key:
            out.setdefault('asked', []).append(key)
            state = _load(life)
    return out


# ---- the owner's answer -----------------------------------------------------------------------------------------------------

def _chat_store(life):
    store = os.environ.get('LIFE_CHAT_DB') or None
    if store is None and Path(life).resolve() != LIVE_LIFE.resolve():
        return None                                   # a sandbox never reads the owner's real chat
    import chats as C
    return store or C.STORE


def show_extra(p: dict, key: str, state: str) -> dict:
    if state not in ('surfaced', 'ready') or p.get('closed'):
        return {}
    return {'when_he_says_yes': f'Run: life approve {key} "<their exact words>". Code checks their words and sets their clock for the trip. '
                                'Do not use life zone for this one. A clear yes in any words is a yes; an "ok" or a thank-you alone is not.',
            'when_he_says_no': f'Run: life drop {key} "<their words>". Nothing changes and this trip is not asked about again.'}


def _vocab(p: dict) -> set:
    import hisreply as H
    t = p['travel']
    first, last = date.fromisoformat(t['first']), date.fromisoformat(t['last'])
    days = [d.strftime('%A') for d in _days(first, last)][:8]
    return H.vocabulary(p.get('title'), t.get('ask'), t.get('place'), OT.label(ZoneInfo(t['zone'])), days,
                        ['time', 'clock', 'zone', 'trip', 'hours', 'quiet', 'note', 'there', 'while', 'away', 'switch', 'put', 'keep'])


def _seen_text(con, p: dict) -> str:
    """What the owner was sent: the note the ask rode in (outbox), or what chat said when it was told there, or the ask itself."""
    told = p.get('told') or {}
    note = told.get('note')
    if note:
        row = con.execute('SELECT text FROM outbox WHERE key=?', (note,)).fetchone()
        if row and row[0]:
            return str(row[0])
    return str(told.get('said') or p['travel'].get('ask') or '')


def approve(con, r, p: dict, words: str, life, close, read_chat=None, now: float | None = None) -> int:
    """`life approve <travel item> "<the owner's words>"` (lifecli). Prints what the chat Zeus should do or tell them."""
    import hisreply as H
    import lastsaid as LS
    import lifezone as Z
    life = Path(life)
    key, t = r['key'], p['travel']
    now = time.time() if now is None else float(now)
    if not on(life):
        print('Trip clock asks are switched off, so nothing was changed. If they say where they are, use life zone with their words.')
        return 2
    if p.get('closed') or r['state'] not in ('surfaced', 'ready'):
        print('This trip question is not waiting on them any more, so nothing was changed. If they say where they are, use life zone with their words.')
        return 3
    if r['state'] == 'ready' or not (p.get('told') or {}).get('at'):
        print('This trip question has not been put to them yet, so their words cannot be an answer to it. Nothing was changed.')
        return 3
    if now >= float(t['departure']):
        print('They did not answer before they left, so nothing was changed and this trip is not asked about again. '
              'If they say where they are, use life zone with their words.')
        return 3
    if len(str(words or '').strip()) < 1:
        print(f'Quote what they said: life approve {key} "<their exact words>". Nothing was changed.')
        return 2
    told_at = float(p['told']['at'])
    store = _chat_store(life)
    try:
        if read_chat is None:
            if store is None:
                raise RuntimeError('a sandbox has no chat store of its own')
            import chats as C
            read_chat = lambda since: C.messages(since, path=store)
        msgs = [m for m in read_chat(told_at - 1) if m['who'] == 'owner' and float(m['at']) <= now + 1]
    except Exception as e:
        print(f'Could not check their words against their real messages ({type(e).__name__}), so nothing was changed.')
        return 1
    import chats as C
    # The quoted words only help pick among the owner's messages since the ask; when they match none (a tidied typo, a
    # short "y"), their newest one is read. What they really wrote decides, never the quote (the same rule as onesend.owners_reply).
    msg = C.quoted_in(words, msgs) or (msgs[-1] if msgs else None)
    if msg is None:
        print('They have sent no message since they were asked about the trip, so nothing was changed. If they did answer, ask them once more.')
        return 3
    for m in msgs:
        if (m['at'], m['id']) > (msg['at'], msg['id']):
            later = H.read(m['text'])
            if later['answer'] in ('no', 'changed') or str(later['why']).endswith('not decided yet'):
                print(f'After that they wrote: "{m["text"][:200]}". So that yes does not stand, and nothing was changed. Go by their newest message.')
                return 9
    others = {k: v for k, v in LS.open_asks(con).items() if k != key}
    reading = H.bind(H.read(msg['text']), _vocab(p), [H.vocabulary(*v) for v in others.values()])
    try:
        last = LS.last(con, float(msg['at']), store=store) if store else None
    except Exception:
        last = None
    fresh_this = bool(last and last['asks'] == [key] and float(msg['at']) - last['at'] <= JUST_ASKED)
    answer = reading['answer']
    if answer == 'other' and H.agrees(msg['text']):
        between = [m for m in msgs if last and last['at'] <= m['at'] and (m['at'], m['id']) < (msg['at'], msg['id'])]
        answer = 'yes' if fresh_this and last['question'] and not between else 'other'
    elif answer in ('yes', 'no') and others and not reading.get('points') and not fresh_this:
        print('They have more than one thing waiting on their word and this answer does not say which. Ask them once, in a few words, '
              f'whether they mean the {t["place"]} trip clock. Nothing was changed.')
        return 2
    if answer == 'no':
        close(con, r, 'declined', 'zeus chat, the owner\'s no to a trip clock', 'they said no: their clock stays on home time')
        _mark(life, t['trip'], state='no', owners_words=msg['text'][:300])
        print('Recorded their no. Their clock stays on home time and this trip is not asked about again.')
        return 0
    if answer == 'changed':
        print(f'Their answer has a change in it ({reading.get("why")}), so the clock was not moved. Ask them in one short line what they want instead. '
              'Nothing was changed.')
        return 2
    if answer != 'yes':
        print('That is not a yes or a no to the trip clock, so nothing was changed. Leave it open.')
        return 2
    zone, place = t['zone'], t['place']
    seen = _seen_text(con, p)
    if not Z._named_in(seen, place, zone):
        print(f'The message they answered did not say where the trip is, so the clock was not moved. Ask them once, naming {place}. Nothing was changed.')
        return 3
    first, last_day = date.fromisoformat(t['first']), date.fromisoformat(t['last'])
    today = datetime.fromtimestamp(now, ZoneInfo(zone)).date()
    rec = OT.set_zone(life, zone, place=place, until=last_day, owners_words=msg['text'][:300], message_id=msg.get('id'),
                      starts=first if first > today else None,
                      asked={'item': key, 'ask': t.get('ask'), 'seen': seen[:600], 'told_at': told_at, 'by': 'travel ask'})
    Z._settle_now(life)
    close(con, r, 'done', 'zeus chat, the owner\'s yes to a trip clock', 'they said yes: their clock follows them on this trip', {'zone_set': rec})
    _mark(life, t['trip'], state='yes', owners_words=msg['text'][:300])
    import enddate as E
    label = OT.label(ZoneInfo(zone))
    print(f'Done. From {E.plain_day(first, today)} through {E.plain_day(last_day, today)} their quiet hours follow {label} time '
          f'({place}), then go back to home time by themselves. Tell them that in one short line. If their plans change: life zone home')
    return 0


# ---- read-only look (the replay) ----------------------------------------------------------------------------------------

def preview(life, snapshot=None, days: int = 30, now: float | None = None) -> dict:
    """What asks would be made in the next `days`, without writing anything. For a replay on copies."""
    now = OT._clock() if now is None else float(now)
    events, ok, why = read_snapshot(snapshot)
    trips, left = detect(events, now, life)
    soon = [t for t in trips if t['departure'] <= now + days * 86400]
    return {'snapshot_ok': ok, 'why': why, 'events': len(events),
            'asks': [{'item': f"travel:{_slug(t['place'])}:{t['first']}", 'ask': ask_text(t), 'headline': packet_for(t)['headline'],
                      'how': t['how'], 'evidence': t['evidence'], 'asked_from': 'the day before', 'departure':
                      datetime.fromtimestamp(t['departure'], OT.zone(at=t['departure'], life=life)).isoformat()} for t in soon],
            'left_out': left}
