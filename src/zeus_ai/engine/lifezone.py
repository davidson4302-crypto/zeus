"""lifezone: `life zone`, how Zeus, in the owner's chat, records where the owner is.

  life zone                                   where the owner is treated as being right now
  life zone <city or zone> [until <day>] --said "<the owner's exact words>"
                                              the owner is away: quiet hours follow the clock there
  life zone home                              the owner is back: home time again

Why the owner's words are needed: this setting moves the hours in which the owner may be texted. A wrong one is a text
at 6am. So it is only ever recorded against a real recent message of theirs that names the place or the zone, the same
bar as a standing OK (`life grant`). Going back to home time needs no words: that is the engine's default.

Why there is always a last day: a setting that is forgotten would keep the owner on a travel clock after they are home.
With no day given the last day is a week out, and never more than MAX_DAYS; the reply says so.

It refuses what it does not know: a place it cannot put on one clock (any real city, country or US state is known; a
name that fits two clocks is asked about once, with the choices named), a day it cannot read, words that are not the
owner's. A refusal changes nothing. No automatic detection from flights or mail here. (travel.py, switch
travel-ask-on: code may notice a trip on the owner's calendar and ASK once; only their yes to that ask, which names the
place, sets the clock, through the same ownertime.set_zone, from the trip's first day.)
The tool's replies are read by Zeus, which tells the owner in its own words.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import enddate as E
import ownertime as OT
import settings


def _home() -> str:
    """The plain name of the owner's home clock (\"Pacific\", \"Eastern\", ...)."""
    try:
        return OT.label(ZoneInfo(OT.HOME))
    except Exception:
        return 'home'


def _hour(h: int) -> str:
    return '%d%s' % (h % 12 or 12, 'am' if h < 12 else 'pm')

NEEDS_HIS_WORDS = True     # False would let Zeus set a zone on its own say-so. Left on: a wrong zone is a text at the wrong hour
DEFAULT_DAYS = 7
MAX_DAYS = 60
E_, C_, M_, P_ = 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles'
# Ordinary names people use: common travel cities and the plain zone words. Anything else must be
# a real zone name ("Europe/London") or it is refused.
PLACES = {
    'eastern': E_, 'east coast': E_, 'et': E_, 'est': E_, 'edt': E_, 'new york': E_, 'nyc': E_, 'boston': E_, 'miami': E_, 'orlando': E_,
    'tampa': E_, 'atlanta': E_, 'washington': E_, 'dc': E_, 'philadelphia': E_, 'charlotte': E_, 'raleigh': E_, 'detroit': E_,
    'knoxville': E_, 'pittsburgh': E_, 'cleveland': E_, 'columbus': E_, 'cincinnati': E_, 'baltimore': E_, 'fort lauderdale': E_,
    'jacksonville': E_, 'indianapolis': 'America/Indiana/Indianapolis', 'louisville': 'America/Kentucky/Louisville',
    'central': C_, 'ct': C_, 'cst': C_, 'cdt': C_, 'chicago': C_, 'nashville': C_, 'memphis': C_, 'dallas': C_, 'houston': C_, 'austin': C_,
    'san antonio': C_, 'new orleans': C_, 'minneapolis': C_, 'st louis': C_, 'saint louis': C_, 'kansas city': C_, 'oklahoma city': C_,
    'milwaukee': C_, 'omaha': C_, 'tulsa': C_, 'birmingham': C_,
    'mountain': M_, 'mt': M_, 'mst': M_, 'mdt': M_, 'denver': M_, 'salt lake city': M_, 'salt lake': M_, 'boise': 'America/Boise',
    'albuquerque': M_, 'colorado springs': M_,
    'arizona': 'America/Phoenix', 'phoenix': 'America/Phoenix', 'scottsdale': 'America/Phoenix', 'tucson': 'America/Phoenix',
    'pacific': P_, 'west coast': P_, 'pt': P_, 'pst': P_, 'pdt': P_, 'los angeles': P_, 'la': P_, 'san francisco': P_, 'san diego': P_,
    'seattle': P_, 'portland': P_, 'las vegas': P_, 'vegas': P_, 'sacramento': P_, 'san jose': P_, 'oakland': P_, 'california': P_,
    'hawaii': 'Pacific/Honolulu', 'honolulu': 'Pacific/Honolulu', 'maui': 'Pacific/Honolulu', 'kauai': 'Pacific/Honolulu', 'oahu': 'Pacific/Honolulu',
    'kona': 'Pacific/Honolulu', 'alaska': 'America/Anchorage', 'anchorage': 'America/Anchorage',
    'toronto': 'America/Toronto', 'vancouver': 'America/Vancouver', 'mexico city': 'America/Mexico_City', 'cancun': 'America/Cancun',
    'cabo': 'America/Mazatlan', 'london': 'Europe/London', 'paris': 'Europe/Paris', 'amsterdam': 'Europe/Amsterdam', 'tokyo': 'Asia/Tokyo',
}
HOME_WORDS = ('home', 'back', 'back home', 'reset', 'default')
SHORT = ('et', 'est', 'edt', 'ct', 'cst', 'cdt', 'mt', 'mst', 'mdt', 'pt', 'pst', 'pdt', 'nyc', 'dc', 'la')
STATES = {'tennessee': None, 'texas': None, 'florida': None, 'kentucky': None, 'indiana': None}   # states that span two zones: named so the refusal can say why


def _flat(s) -> str:
    return re.sub(r'\s+', ' ', re.sub(r"[^a-z0-9/_+-]+", ' ', _fold(s).lower().replace('.', ''))).strip()   # accents off


def lookup(words):
    """-> (zone name, the place as the owner would say it), or None when it is not known or fits two clocks. A real
    zone name is taken as is. Any real city, country or US state resolves (resolve, below)."""
    found = resolve(words)
    return found[1:] if found and found[0] == 'zone' else None

# Any real place resolves on its own, so a city missing from the table above is never refused: every city in this
# Mac's own time zone list ("Europe/Lisbon" is Lisbon), every country with one main clock (this Mac's country list),
# every US state, and a short list of well-known cities that are not zone names. A name that fits two clocks is asked
# about once, with the choices named: Portland, Birmingham, Georgia, Washington, Springfield, and any state or country
# that spans zones ("Portland Maine", "Perth Australia" then resolve). The table above still wins where it has the name.
US_STATES = {
    'alabama': C_, 'alaska': 'America/Anchorage', 'arizona': 'America/Phoenix', 'arkansas': C_, 'california': P_, 'colorado': M_,
    'connecticut': E_, 'delaware': E_, 'florida': None, 'hawaii': 'Pacific/Honolulu', 'idaho': None, 'illinois': C_, 'indiana': None,
    'iowa': C_, 'kansas': C_, 'kentucky': None, 'louisiana': C_, 'maine': E_, 'maryland': E_, 'massachusetts': E_, 'michigan': E_,
    'minnesota': C_, 'mississippi': C_, 'missouri': C_, 'montana': M_, 'nebraska': None, 'nevada': P_, 'new hampshire': E_,
    'new jersey': E_, 'new mexico': M_, 'new york state': E_, 'north carolina': E_, 'north dakota': None, 'ohio': E_, 'oklahoma': C_,
    'oregon': P_, 'pennsylvania': E_, 'rhode island': E_, 'south carolina': E_, 'south dakota': None, 'tennessee': None,
    'texas': None, 'utah': M_, 'vermont': E_, 'virginia': E_, 'washington state': P_, 'west virginia': E_, 'wisconsin': C_,
    'wyoming': M_, 'puerto rico': 'America/Puerto_Rico',
}
# The two clocks of a state that spans zones, as a city on each side, so the question can name them.
STATE_SIDES = {'florida': (('Miami', E_), ('Pensacola', C_)), 'idaho': (('Boise', M_), ("Coeur d'Alene", P_)),
               'indiana': (('Indianapolis', E_), ('Evansville', C_)), 'kentucky': (('Louisville', E_), ('Bowling Green', C_)),
               'nebraska': (('Omaha', C_), ('Scottsbluff', M_)), 'north dakota': (('Fargo', C_), ('Dickinson', M_)),
               'south dakota': (('Sioux Falls', C_), ('Rapid City', M_)), 'tennessee': (('Nashville', C_), ('Knoxville', E_)),
               'texas': (('Dallas', C_), ('El Paso', M_))}
ALIASES = {
    # well-known cities whose clock has another city's name
    'barcelona': 'Europe/Madrid', 'seville': 'Europe/Madrid', 'sevilla': 'Europe/Madrid', 'valencia spain': 'Europe/Madrid',
    'malaga': 'Europe/Madrid', 'ibiza': 'Europe/Madrid', 'mallorca': 'Europe/Madrid', 'porto': 'Europe/Lisbon', 'faro': 'Europe/Lisbon',
    'milan': 'Europe/Rome', 'florence': 'Europe/Rome', 'venice': 'Europe/Rome', 'naples': 'Europe/Rome', 'tuscany': 'Europe/Rome',
    'munich': 'Europe/Berlin', 'frankfurt': 'Europe/Berlin', 'hamburg': 'Europe/Berlin', 'cologne': 'Europe/Berlin',
    'edinburgh': 'Europe/London', 'manchester': 'Europe/London', 'glasgow': 'Europe/London', 'liverpool': 'Europe/London',
    'lyon': 'Europe/Paris', 'marseille': 'Europe/Paris', 'bordeaux': 'Europe/Paris', 'geneva': 'Europe/Zurich', 'krakow': 'Europe/Warsaw',
    'salzburg': 'Europe/Vienna', 'the hague': 'Europe/Amsterdam', 'rotterdam': 'Europe/Amsterdam', 'antwerp': 'Europe/Brussels',
    'bruges': 'Europe/Brussels', 'santorini': 'Europe/Athens', 'mykonos': 'Europe/Athens', 'dubrovnik': 'Europe/Zagreb',
    'split': 'Europe/Zagreb', 'osaka': 'Asia/Tokyo', 'kyoto': 'Asia/Tokyo', 'beijing': 'Asia/Shanghai', 'china': 'Asia/Shanghai',
    'delhi': 'Asia/Kolkata', 'new delhi': 'Asia/Kolkata', 'mumbai': 'Asia/Kolkata', 'bangalore': 'Asia/Kolkata', 'bengaluru': 'Asia/Kolkata',
    'tel aviv': 'Asia/Jerusalem', 'abu dhabi': 'Asia/Dubai', 'bali': 'Asia/Makassar', 'phuket': 'Asia/Bangkok', 'hanoi': 'Asia/Bangkok',
    'saigon': 'Asia/Ho_Chi_Minh', 'cape town': 'Africa/Johannesburg', 'rio': 'America/Sao_Paulo', 'rio de janeiro': 'America/Sao_Paulo',
    'punta cana': 'America/Santo_Domingo', 'tulum': 'America/Cancun', 'playa del carmen': 'America/Cancun',
    'puerto vallarta': 'America/Mexico_City', 'cabo san lucas': 'America/Mazatlan', 'whistler': 'America/Vancouver',
    'montreal': 'America/Toronto', 'ottawa': 'America/Toronto', 'quebec': 'America/Toronto', 'calgary': 'America/Edmonton',
    'banff': 'America/Edmonton', 'washington dc': E_, 'charleston': E_, 'savannah': E_, 'key west': E_, 'myrtle beach': E_,
    'gatlinburg': E_, 'chattanooga': E_, 'el paso': M_, 'aspen': M_, 'park city': M_, 'jackson hole': M_, 'sedona': 'America/Phoenix',
    'palm springs': P_, 'lake tahoe': P_, 'tahoe': P_, 'napa': P_, 'santa barbara': P_, 'reno': P_, 'spokane': P_,
    'uk': 'Europe/London', 'england': 'Europe/London', 'scotland': 'Europe/London', 'wales': 'Europe/London',
    'great britain': 'Europe/London', 'south korea': 'Asia/Seoul', 'korea': 'Asia/Seoul', 'holland': 'Europe/Amsterdam',
    'czechia': 'Europe/Prague', 'uae': 'Asia/Dubai', 'the netherlands': 'Europe/Amsterdam',
}
# Names that fit two clocks: (how the owner would say each one, its zone, the words that pick it).
AMBIGUOUS = {
    'portland': (('Portland, Oregon', P_, ('oregon', 'or')), ('Portland, Maine', E_, ('maine', 'me'))),
    'birmingham': (('Birmingham, Alabama', C_, ('alabama', 'al')), ('Birmingham, England', 'Europe/London', ('england', 'uk'))),
    'georgia': (('Georgia, the state', E_, ('state', 'atlanta', 'usa', 'us')), ('Georgia, the country', 'Asia/Tbilisi', ('country', 'tbilisi'))),
    'washington': (('Washington, DC', E_, ('dc', 'd c')), ('Washington state', P_, ('state', 'wa', 'seattle'))),
    'springfield': (('Springfield, Illinois or Missouri', C_, ('illinois', 'il', 'missouri', 'mo')),
                    ('Springfield, Massachusetts', E_, ('massachusetts', 'ma'))),
}
WORLD = ('Africa', 'America', 'Asia', 'Atlantic', 'Australia', 'Europe', 'Indian', 'Pacific')
NOT_PLACES = frozenset(('christmas', 'easter', 'wake', 'center', 'troll', 'knox', 'vevay', 'marengo', 'winamac', 'tell city', 'beulah'))
_ATLAS = None


def _fold(s) -> str:
    """'São Paulo' -> 'Sao Paulo': accents off, so the owner's spelling and the zone list's spelling meet."""
    import unicodedata
    return ''.join(ch for ch in unicodedata.normalize('NFKD', str(s or '')) if not unicodedata.combining(ch))


def _clock_pair(key: str) -> tuple:
    """The zone's offset in January and in July: two zones with the same pair keep the same clock all year."""
    year = datetime.fromtimestamp(OT._clock()).year
    z = ZoneInfo(key)
    return tuple(datetime(year, m, 15, 12, tzinfo=z).utcoffset() for m in (1, 7))


def _atlas() -> dict:
    """flat name -> ('zone', key, shown) or ('choose', shown, [(shown, key), ...]): this Mac's own zone list and country
    list, read once. Anything that cannot be read is simply not in it."""
    global _ATLAS
    if _ATLAS is not None:
        return _ATLAS
    import zoneinfo
    out = {}
    cities = {}
    for key in sorted(zoneinfo.available_timezones()):
        parts = key.split('/')
        if parts[0] not in WORLD or len(parts) > 3 or (len(parts) == 3 and parts[1] != 'Argentina'):
            continue
        shown = parts[-1].replace('_', ' ')
        cities.setdefault(_flat(shown), (shown, []))[1].append(key)
    for name, (shown, keys) in cities.items():
        if name in NOT_PLACES or len(name) < 3:
            continue
        try:
            same = len({_clock_pair(k) for k in keys}) == 1
        except Exception:
            continue
        out[name] = ('zone', min(keys, key=len), shown) if same else ('choose', shown, [(k.replace('_', ' '), k) for k in keys])
    names, zones = {}, {}
    for folder in zoneinfo.TZPATH:
        try:
            for line in (Path(folder) / 'iso3166.tab').read_text(encoding='utf-8').splitlines():
                if line and not line.startswith('#') and '\t' in line:
                    code, name = line.split('\t', 1)
                    names[code] = name.strip()
            for line in (Path(folder) / 'zone.tab').read_text(encoding='utf-8').splitlines():
                if line and not line.startswith('#'):
                    cols = line.split('\t')
                    if len(cols) >= 3:
                        zones.setdefault(cols[0], []).append((cols[2], cols[3] if len(cols) > 3 else ''))
            break
        except OSError:
            names, zones = {}, {}
    for code, name in names.items():
        found = zones.get(code) or []
        if not found:
            continue
        main = [k for k, note in found if re.search(r'mainland|most of|most areas|most locations', note, re.I)]
        try:
            one = len(found) == 1 or len({_clock_pair(k) for k, _ in found}) == 1
        except Exception:
            continue
        pick = found[0][0] if one else main[0] if len(main) == 1 else None
        shown = re.sub(r'\s*\(.*\)$', '', name)
        named = [k for k in main] if len(main) >= 2 else [k for k, _ in found]      # the main clocks first, when the list says which
        entry = ('zone', pick, shown) if pick else ('choose', shown, [(k.split('/')[-1].replace('_', ' '), k) for k in named]
                                                    + [(k.split('/')[-1].replace('_', ' '), k) for k, _ in found if k not in named])
        for alias in {_flat(shown), _flat(name)}:
            if alias and alias not in out and alias not in PLACES:
                out[alias] = entry
        for bits in re.findall(r'^(.*?)\s*\((North|South)\)$', name):      # "Korea (South)" is also "South Korea"
            out.setdefault(_flat(bits[1] + ' ' + bits[0]), entry)
    out.pop('us', None)          # "us" is a word before it is a country
    _ATLAS = out
    return out


def resolve(words):
    """-> ('zone', zone name, the place as the owner would say it), ('choose', the place, [(each choice, its zone), ...]) when
    the name fits more than one clock, or None when it is not a place this knows. Never a guess between two clocks."""
    w = _flat(_fold(words))
    if not w:
        return None
    raw = str(words).strip()
    if '/' in raw:
        try:
            return ('zone', ZoneInfo(raw).key, raw.split('/')[-1].replace('_', ' '))
        except Exception:
            return None
    w = re.sub(r'^(?:the city of|downtown|greater)\s+', '', w)
    shown = ' '.join(x.upper() if x in SHORT or x in ('uk', 'usa', 'uae') else x.capitalize() for x in w.split())
    if w in AMBIGUOUS:
        return ('choose', shown, [(label, zone) for label, zone, _ in AMBIGUOUS[w]])
    if w in PLACES:
        return ('zone', PLACES[w], shown)
    if w in ALIASES:
        return ('zone', ALIASES[w], shown)
    if w in US_STATES or w + ' state' in US_STATES:
        zone = US_STATES.get(w) or US_STATES.get(w + ' state')
        if zone:
            return ('zone', zone, shown)
        return ('choose', shown, list(STATE_SIDES.get(w, ())))
    try:
        found = _atlas().get(w)
    except Exception:
        found = None
    if found:
        return found
    parts = w.split()
    for cut in range(len(parts) - 1, 0, -1):          # "portland maine", "lisbon portugal", "perth australia", "austin texas"
        head, tail = ' '.join(parts[:cut]), ' '.join(parts[cut:])
        if head in AMBIGUOUS:
            for label, zone, picks in AMBIGUOUS[head]:
                if tail in picks or tail in _flat(label).split():
                    return ('zone', zone, label)
            continue
        if tail in AMBIGUOUS or not (tail in US_STATES or tail in ALIASES or tail in _atlas()):
            continue
        where, got = resolve(tail), resolve(head)
        full = ' '.join(x.capitalize() for x in parts)
        if where and where[0] == 'zone':
            return ('zone', where[1], full)           # the region named decides ("san jose costa rica")
        if where and got and got[0] == 'zone':
            if _clock_pair(got[1]) in {_clock_pair(z) for _, z in where[2] if z}:
                return ('zone', got[1], full)          # the city is one of that region's clocks
        if where:
            for label, zone in where[2]:
                if head in _flat(label).split() and zone:
                    return ('zone', zone, full)
    return None


def _names_for(zone_key: str) -> set:
    """Every ordinary word for this zone: its places and its plain name."""
    return {k for k, v in PLACES.items() if v == zone_key} | {OT.label(ZoneInfo(zone_key)).lower()}


def _named_in(owners_words: str, place: str, zone_key: str) -> bool:
    text = ' ' + _flat(owners_words) + ' '
    bits = {w for w in _flat(place).split() if len(w) > 3 and w not in ('city', 'state', 'time', 'zone', 'county', 'united')}   # "maine" names "Portland, Maine"
    return any((' ' + n + ' ') in text for n in _names_for(zone_key) | {_flat(place)} | bits if len(n) > 2)


def _his_message(words: str):
    """The owner's real message (last 48 hours) that holds these words, or None. Same reader `life grant` uses."""
    import chats as C
    msgs = C.messages(time.time() - 48 * 3600, path=os.environ.get('LIFE_CHAT_DB') or None)
    return C.quoted_in(words, msgs)


def _clock12(t: datetime) -> str:
    return '%d%s%s' % (t.hour % 12 or 12, ':%02d' % t.minute if t.minute else '', 'am' if t.hour < 12 else 'pm')


def _pending(r) -> bool:
    """A trip the owner said yes to whose first day has not come yet (owner-zone.json has a `from`)."""
    return bool(r and r.get('starts') and OT._clock() < r['starts'] and (r['ends'] is None or OT._clock() < r['ends']))


def where(life) -> str:
    r = OT.rule(life)
    z = OT.zone(life=life)
    if _pending(r):
        today = datetime.fromtimestamp(OT._clock(), r['zone']).date()
        text = ('The owner is treated as being at home, on %s time, for now. From %s they are on %s time (%s)'
                % (_home(), E.plain_day(r['from'], today), OT.label(r['zone']), r['place'] or OT.label(r['zone'])))
        if r['until']:
            text += ' through %s, then back on %s' % (E.plain_day(r['until'], today), _home())
        return text + '.'
    if r is None or z.key == OT.HOME:
        return 'The owner is treated as being at home, on %s time.' % _home()
    today = datetime.fromtimestamp(OT._clock(), z).date()
    text = 'The owner is treated as being on %s time (%s)' % (OT.label(z), r['place'] or OT.label(z))
    if r['until']:
        text += ' through %s, then back on %s' % (E.plain_day(r['until'], today), _home())
    return text + '.' + (' They said: "%s"' % r['owners_words'] if r.get('owners_words') else '')


def _settle_now(life: Path):
    """Rewrite the stored times of open items on the new clock right away. If the list is busy, the next run does it."""
    p = life / 'ledger.sqlite3'
    if not p.exists():
        return
    try:
        with closing(sqlite3.connect(p, timeout=15)) as con:
            OT.settle(con, life)
    except sqlite3.Error:
        pass


def main(a: list, life, owners_message=None) -> int:
    life = Path(life)
    a = list(a)
    said = ''
    if '--said' in a:
        i = a.index('--said')
        said = ' '.join(a[i + 1:i + 2]).strip()
        del a[i:i + 2]
    words = ' '.join(a).strip()
    if not words:
        print(where(life))
        return 0
    if _flat(words) in HOME_WORDS:
        if _pending(OT.rule(life)):        # a trip clock set for later: "home" calls it off
            OT.set_zone(life, OT.HOME, place='home')
            _settle_now(life)
            print('The switch to another clock planned for the owner\'s trip is called off. They stay on %s time.' % _home())
            return 0
        if OT.rule(life) is None or OT.zone(life=life).key == OT.HOME:
            print('The owner is already treated as being at home, on %s time. Nothing changed.' % _home())
            return 0
        OT.set_zone(life, OT.HOME, place='home')
        _settle_now(life)
        print('Back on %s time. Quiet hours follow the clock at home again.' % _home())
        return 0
    place_words, _, until_words = re.sub(r'\s+', ' ', words).partition(' until ')
    for alt in (' through ', ' till ', ' thru '):
        if not until_words and alt in place_words:
            place_words, _, until_words = place_words.partition(alt)
    found = resolve(place_words)      # any real place; a name on two clocks is asked about once
    if found is not None and found[0] == 'choose':
        names = [c for c, _ in found[2]][:4]
        many = 'two time zones' if len(found[2]) == 2 else 'more than one time zone'
        print(f'{found[1]} is on {many} ({", ".join(names[:-1])} or {names[-1]}), so nothing was changed. Ask the owner once, in a '
              f'few words, which one they mean, then run this again with the one they name (for example: life zone {names[0]} ...). '
              'Do not ask them to name a city: they just named one.')
        return 2
    found = found[1:] if found else None
    if found is None:
        print(f'I do not know which time zone "{place_words}" is in, so nothing was changed: the owner\'s quiet hours stay on home time '
              f'({_home()}) for now. Tell them that plainly in one line, and ask once which big city or time zone it is near; '
              'then run this again with their answer. Never ask them to name "a city you know": they just named one.')
        return 2
    zone_key, place = found
    if zone_key == OT.HOME:
        print('That is %s time, the same as home. If the owner is back, use: life zone home. Nothing was changed.' % _home())
        return 2
    z = ZoneInfo(zone_key)
    today = datetime.fromtimestamp(OT._clock(), z).date()
    picked = False
    if until_words.strip():
        until = E.end_day('until ' + until_words, today)
        if until is None:
            print(f'I could not read the day "{until_words.strip()}", so nothing was changed. Give a weekday (Friday), a date (November 20 or YYYY-MM-DD) or tomorrow.')
            return 2
        if (until - today).days > MAX_DAYS:
            print(f'That is more than {MAX_DAYS} days away, so nothing was changed. Set it for a shorter stretch and set it again later.')
            return 2
    else:
        until, picked = today + timedelta(days=DEFAULT_DAYS), True
    if not NEEDS_HIS_WORDS and not said:
        OT.set_zone(life, zone_key, place=place, until=until)
        _settle_now(life)
        print(f'Done. The owner is now treated as being on {OT.label(z)} time ({place}) through {E.plain_day(until, today)}, then back on {_home()}. '
              'If they get home sooner: life zone home')
        return 0
    if len(said) < 6:
        print(f'Quote what the owner said about being there: life zone {place_words}' + (f' until {until_words.strip()}' if until_words.strip() else '')
              + ' --said "<the owner\'s exact words>". This moves the hours the owner can be texted, so it is only set from their own words. Nothing was changed.')
        return 2
    try:
        msg = (owners_message or _his_message)(said)
    except Exception as e:
        print(f'Could not check the owner\'s words against their real messages ({type(e).__name__}), so nothing was changed.')
        return 1
    if msg is None:
        print('Those words are not in any message the owner sent in the last two days, so nothing was changed. Quote them exactly, or ask where they are.')
        return 3
    if not _named_in(said, place, zone_key):
        print(f'The owner\'s words do not name {place} or {OT.label(z)} time, so nothing was changed. Quote the message where they say where they are, or ask them.')
        return 3
    OT.set_zone(life, zone_key, place=place, until=until, owners_words=said[:300], message_id=msg.get('id'))
    _settle_now(life)
    text = (f'Done. The owner is now treated as being on {OT.label(z)} time ({place}) through {E.plain_day(until, today)}, then back on {_home()}. '
            f'Quiet hours follow the clock where they are: nothing before {_hour(settings.QUIET_END)} there, nothing routine after {_hour(settings.QUIET_START)} there.')
    if picked:
        text += ' They gave no last day, so I picked a week. If they are away longer, set it again.'
    if OT.LINE_CLOCK and not OT.line_follows():   # with zone-on the engine's line counts its morning where the owner is (ownertime.owners_clock_line)
        nine = datetime.fromtimestamp(OT._clock(), ZoneInfo(OT.LINE_CLOCK)).replace(hour=OT.LINE_DAY_START, minute=0, second=0, microsecond=0)
        there = nine.astimezone(z)
        if there.hour > OT.LINE_DAY_START:
            text += (f' One limit for now: the texting line still keeps home-time mornings, so the earliest a note can reach the owner is '
                     f'{_clock12(there)} their time.')
    print(text + ' If they get home sooner: life zone home')
    return 0
