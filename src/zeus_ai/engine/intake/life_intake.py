"""Life-event intake.

One email in -> a class (actionable_automated | human | junk) and, for actionable mail, a typed event:
flight, rx, invite, bill, hotel, delivery, appointment. Pure functions, no I/O, so tests run offline.

Deterministic rules first (cheap, explainable). Each result carries `confidence` and `why`, so a later model step
can take only the unsure ones. Never invents a field: anything not found stays None.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # engine/, for settings
import settings  # noqa: E402

OWNER_TZ = ZoneInfo(settings.OWNER_TZ)   # the owner's clock: the default for times that carry no zone
PACIFIC = OWNER_TZ                       # older name, kept for callers that still import it

AIRLINES = {  # sender domain -> (carrier, IATA code)
    'southwest.com': ('Southwest', 'WN'), 'delta.com': ('Delta', 'DL'), 'aa.com': ('American', 'AA'),
    'united.com': ('United', 'UA'), 'alaskaair.com': ('Alaska', 'AS'), 'jetblue.com': ('JetBlue', 'B6'),
    'spirit.com': ('Spirit', 'NK'), 'flyfrontier.com': ('Frontier', 'F9'), 'hawaiianairlines.com': ('Hawaiian', 'HA'),
    'aeromexico.com': ('Aeromexico', 'AM'), 'volaris.com': ('Volaris', 'Y4'), 'allegiantair.com': ('Allegiant', 'G4'),
}
CARRIER_NAMES = [('southwest', 'Southwest', 'WN'), ('delta', 'Delta', 'DL'), ('american airlines', 'American', 'AA'),
                 ('united', 'United', 'UA'), ('alaska airlines', 'Alaska', 'AS'), ('jetblue', 'JetBlue', 'B6'),
                 ('spirit', 'Spirit', 'NK'), ('frontier airlines', 'Frontier', 'F9'), ('hawaiian', 'Hawaiian', 'HA'),
                 ('aeromexico', 'Aeromexico', 'AM'), ('aeroméxico', 'Aeromexico', 'AM'), ('volaris', 'Volaris', 'Y4'),
                 ('allegiant', 'Allegiant', 'G4')]
CODES = {c for _, _, c in CARRIER_NAMES}
PHARMACIES = {'cvs.com': 'CVS', 'walgreens.com': 'Walgreens', 'riteaid.com': 'Rite Aid', 'express-scripts.com': 'Express Scripts',
              'optum.com': 'Optum Rx', 'optumrx.com': 'Optum Rx', 'kp.org': 'Kaiser Permanente', 'costco.com': 'Costco Pharmacy',
              'safeway.com': 'Safeway Pharmacy', 'walmart.com': 'Walmart Pharmacy', 'capsule.com': 'Capsule'}
HOTELS = ('marriott.com', 'hilton.com', 'hyatt.com', 'ihg.com', 'booking.com', 'expedia.com', 'airbnb.com',
          'mgmresorts.com', 'caesars.com', 'wynnlasvegas.com', 'hotels.com', 'choicehotels.com')
CALENDAR_SENDERS = ('calendar-notification@google.com', 'calendar-server.bounces.google.com', 'no-reply@calendar.google.com')
# Airport -> time zone (local times in itineraries are local to each airport).
TZ = {}
for zone, codes in {
    'America/Los_Angeles': 'LAX ONT SNA BUR LGB SAN SJC SFO OAK SMF PSP SBA FAT LAS RNO SEA PDX GEG BLI TIJ SBP MRY',
    'America/Phoenix': 'PHX TUS AZA FLG', 'America/Denver': 'DEN SLC ABQ COS BOI ELP BZN JAC', 'America/Boise': '',
    'America/Chicago': 'ORD MDW DFW DAL IAH HOU AUS SAT MSP STL MCI MSY BNA MEM OKC TUL OMA MKE IND DSM ICT LIT BHM',
    'America/New_York': 'ATL JFK LGA EWR BOS DCA IAD BWI PHL CLT MIA FLL TPA MCO RDU DTW CLE CMH PIT BUF JAX RSW PBI SAV CHS RIC ORF BDL PVD',
    'America/Detroit': '', 'America/Anchorage': 'ANC FAI', 'Pacific/Honolulu': 'HNL OGG KOA LIH',
    'America/Mexico_City': 'GDL MEX MTY BJX QRO PVR', 'America/Cancun': 'CUN', 'America/Mazatlan': 'SJD MZT',
    'America/Toronto': 'YYZ YOW', 'America/Vancouver': 'YVR'}.items():
    for c in codes.split():
        TZ[c] = ZoneInfo(zone)

PROMO = re.compile(r"\b(fares? (?:from|as low as)|sale\b|\d+% off|deals?\b|save up to|limited[- ]time|book now|"
                   r"newsletter|tips\b|explore\b|get the app|flu shots?|vaccines? (?:are )?available|"
                   r"promo(?:tion)? code|special offer|ofertas?|descuento|webinars?|workshops?)", re.I)
STATEMENT = re.compile(r"\b(statement is (?:ready|available)|e-?statement|your (?:monthly )?statement|receipt (?:for|from) |your receipt|"
                       r"thanks for your (?:payment|order)|payment (?:received|confirmation)|bill (?:is )?(?:available|ready)\b(?!.*due)|"
                       r"autopay(?:ment)? (?:is )?scheduled)", re.I)
CODE_STOP = {'NOTICE', 'NUMBER', 'PLEASE', 'CHANGE', 'DETAIL', 'DETAILS', 'STATUS', 'TRAVEL', 'ONLINE', 'EMAIL', 'TICKET',
             'RECORD', 'BOOKED', 'CANCEL', 'UPDATE', 'POLICY', 'REWARD', 'MANAGE', 'NUMERO'}
CONF = re.compile(r"(?:confirmation|record locator|booking|reservation|clave de reservaci[oó]n|c[oó]digo de (?:confirmaci[oó]n|reservaci[oó]n)|"
                  r"localizador)\s*(?:#|number|no\.?|code|n[uú]mero)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{4,14}[A-Z0-9])\b", re.I)
MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
MONTHS.update({'ene': 1, 'abr': 4, 'ago': 8, 'dic': 12, 'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
               'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9, 'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
               'sept': 9})
DATE = re.compile(r"(?:(?P<mon>[A-Za-z]{3,10})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b,?\s*(?P<year>\d{4})?"
                  r"|\b(?P<day2>\d{1,2})\s+(?:de\s+)?(?P<mon2>[A-Za-z]{3,10})\.?(?:\s+(?:de\s+)?(?P<year2>\d{4}))?"
                  r"|\b(?P<y3>\d{4})-(?P<m3>\d{2})-(?P<d3>\d{2})"
                  r"|\b(?P<m4>\d{1,2})/(?P<d4>\d{1,2})/(?P<y4>\d{2,4})"
                  r"|\b(?P<d5>\d{1,2})(?P<mon5>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<y5>\d{2})?\b)", re.I)
TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\.?(?![a-z])|\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?m)", re.I)
MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.\d{2})?)")
NAME_WORD = r"[A-Za-zÁÉÍÓÚÑáéíóúñ'\-]+"


def _domain(addr: str) -> str:
    return (parseaddr(addr or '')[1].rsplit('@', 1)[-1] or '').lower()


def _matches(domain: str, table):
    for d in table:
        if domain == d or domain.endswith('.' + d):
            return d
    return None


def _year_for(month: int, day: int, ref: datetime) -> int:
    y = ref.year
    try:
        cand = datetime(y, month, day)
    except ValueError:
        return y
    return y + 1 if cand < ref.replace(tzinfo=None) - timedelta(days=60) else y


def dates_in(text: str, ref: datetime):
    """All dates in reading order as (date, start, end)."""
    out = []
    pos = 0
    while True:
        m = DATE.search(text, pos)
        if not m:
            break
        pos = m.end()
        try:
            if m['y3']:
                d = datetime(int(m['y3']), int(m['m3']), int(m['d3']))
            elif m['m4']:
                y = int(m['y4']); y = y + 2000 if y < 100 else y
                d = datetime(y, int(m['m4']), int(m['d4']))
            elif m['d5']:
                month, day = MONTHS[m['mon5'].lower()], int(m['d5'])
                year = 2000 + int(m['y5']) if m['y5'] else _year_for(month, day, ref)
                d = datetime(year, month, day)
            else:
                mon = (m['mon'] or m['mon2'] or '').lower()
                key = mon if mon in MONTHS else mon[:3]
                if key not in MONTHS:
                    pos = m.start() + 1  # e.g. "domingo 20 de diciembre": retry from the next character
                    continue
                day = int(m['day'] or m['day2']); month = MONTHS[key]
                year = int(m['year'] or m['year2'] or _year_for(month, day, ref))
                d = datetime(year, month, day)
        except (ValueError, TypeError, KeyError):
            continue
        out.append((d, m.start(), m.end()))
    return out


def times_in(text: str):
    out = []
    for m in TIME.finditer(text):
        if m.group(1):
            h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
            if h > 12:
                continue
            h = (h % 12) + (12 if ap == 'p' else 0)
        else:
            h, mi = int(m.group(4)), int(m.group(5))
        out.append(((h, mi), m.start(), m.end()))
    return out


def _date_after(text, ref, words):
    """First date right after a keyword like 'due date' or 'by'."""
    for m in re.finditer(r'(?:' + words + r')\s*[:\-]?\s*', text, re.I):
        ds = [d for d in dates_in(text[m.end():m.end() + 40], ref) if d[1] <= 12]
        if ds:
            return ds[0][0]
    return None


def html_to_text(raw: str) -> str:
    """Real airline and pharmacy mail is HTML. Drop comments, styles and scripts; turn blocks into lines."""
    if not raw or ('<td' not in raw and '<p' not in raw and '<div' not in raw and '<br' not in raw.lower()):
        return raw or ''
    import html as _html
    t = re.sub(r'(?is)<!--.*?-->|<(style|script|head)\b.*?</\1>', ' ', raw)
    t = re.sub(r'(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|tbody|thead)>', '\n', t)
    t = re.sub(r'(?i)</t[dh]>', '  ', t)
    t = re.sub(r'(?s)<[^>]+>', ' ', t)
    t = _html.unescape(t).replace('\xa0', ' ')
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r' *\n[ \n]*', '\n', t)
    return t.strip()


def _body(email):
    return re.sub(r'[ \t]+', ' ', html_to_text(email.get('body_text') or ''))


def _all(email):
    return f"{email.get('subject') or ''}\n{_body(email)}"


# ----------------------------------------------------------------------------------------------- classify
def classify(email: dict) -> dict:
    """-> {class, event_type, confidence, why}."""
    sender = email.get('from') or ''
    dom = _domain(sender)
    subj = email.get('subject') or ''
    text = _all(email)
    headers = {k.lower(): v for k, v in (email.get('headers') or {}).items()}
    labels = set(email.get('labels') or [])
    bulk = bool(headers.get('list-unsubscribe') or headers.get('list-id')) or bool(labels & {'CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL', 'CATEGORY_FORUMS'})
    updates = 'CATEGORY_UPDATES' in labels
    robot = bool(re.search(r'(no-?reply|do-?not-?reply|notifications?|alerts?|mailer|reminders?|billing|receipts?|support|'
                           r'account|security|service|team|hello|info|news|deals|offers|updates?|reservaciones|reservations)@', sender, re.I)) \
        or bool(re.search(r'automated (?:message|reminder|email)|correo autom[aá]tico|do not reply|unmonitored mailbox', text, re.I))
    forwarded = bool(re.search(r'^\s*(fwd?|fw|rv|reenv[ií]o):', subj, re.I) or 'forwarded message' in text.lower())
    is_air = _matches(dom, AIRLINES)
    is_rx = _matches(dom, PHARMACIES)
    is_hotel = _matches(dom, HOTELS)
    is_cal = any(s in sender.lower() for s in CALENDAR_SENDERS) or 'text/calendar' in headers.get('content-type', '').lower() \
        or 'BEGIN:VCALENDAR' in (email.get('body_text') or '')
    conf = confirmation(email)
    segs = bool(re.search(r'\(([A-Z]{3})\)', _body(email))) or bool(re.search(r'\bflight\s*#?\s*\d{1,4}|\bvuelo\b', text, re.I))

    if re.match(r'^\s*notification:', subj, re.I) and is_cal:
        return _c('junk', None, 0.7, 'calendar reminder for an event already on the calendar')
    if is_cal or re.match(r'^\s*(updated invitation|invitation|canceled event|cancelled event|invitation updated)\b', subj, re.I):
        return _c('actionable_automated', 'invite', 0.95, 'calendar invitation, update or cancellation')
    carrier_named = is_air or any(re.search(r'\b' + re.escape(n) + r'\b', text, re.I) for n, _, _ in CARRIER_NAMES)
    if carrier_named and conf and segs and (is_air or forwarded or robot):
        return _c('actionable_automated', 'flight', 0.9, 'airline confirmation with flight details')
    if is_air and conf and re.search(r'check[- ]?in|boarding|cancel|schedule|change|itinerary|trip', text, re.I) and not _promo(text):
        return _c('actionable_automated', 'flight', 0.8, 'airline service message about a booking')
    if (is_rx or re.search(r'\bpharmacy\b|\bprescription\b|\brx\s*#', text, re.I)) and \
            re.search(r'ready for pick ?up|refill (?:is )?(?:due|reminder|ready)|time to refill|due for (?:a )?refill|next refill|has shipped|'
                      r'prescription (?:is )?ready|order (?:is )?ready', text, re.I) and not re.search(r'flu shot|vaccin', text, re.I):
        return _c('actionable_automated', 'rx', 0.9, 'pharmacy refill or pickup notice')
    if (is_hotel or re.search(r'\bhotel\b|\bresort\b|\bsuites?\b|\binn\b', text, re.I)) and conf and \
            re.search(r'check[- ]?in|arrival|nights?\b', text, re.I) and not _promo(text):
        return _c('actionable_automated', 'hotel', 0.85, 'lodging confirmation')
    if re.search(r"payment (?:was )?(?:failed|declined|unsuccessful)|couldn'?t process your payment|card (?:was )?declined|"
                 r"past due|overdue|amount due|payment due|bill (?:is )?due|invoice (?:is )?due|due date", text, re.I) \
            and not _autopay(text) and (not STATEMENT.search(subj) or re.search(r'amount due|due date|payment due|pay by', text, re.I)):
        return _c('actionable_automated', 'bill', 0.8, 'money due or failed payment')
    if re.search(r'signature required|delivery attempt|missed (?:your )?delivery|requires (?:a )?signature', text, re.I):
        return _c('actionable_automated', 'delivery', 0.75, 'delivery needs action')
    if re.search(r'appointment (?:is )?(?:confirmed|scheduled|reminder)|your appointment', text, re.I) and (robot or bulk):
        return _c('actionable_automated', 'appointment', 0.75, 'appointment confirmation')
    if bulk or robot or updates or _promo(text) or STATEMENT.search(text):
        return _c('junk', None, 0.8, 'bulk, automated or promotional with nothing to act on')
    return _c('human', None, 0.6, 'no automated signals')


def _autopay(text):
    """Autopay will pay it: nothing for the owner to do (unless the mail says they are NOT enrolled or it failed)."""
    if re.search(r"not (?:enrolled|signed up) (?:in|for) auto ?pay|auto ?pay (?:failed|was declined)", text, re.I):
        return False
    return bool(re.search(r"auto ?pay(?:ment)? (?:is |has been )?(?:scheduled|set up|enrolled|on)|will be (?:automatically )?(?:paid|charged|deducted)|"
                          r"automatic payment (?:is )?scheduled|enrolled in auto ?pay|processed using your auto ?pay", text, re.I))


def _promo(text):
    return bool(PROMO.search(text))


def _c(cls, et, conf, why):
    return {'class': cls, 'event_type': et, 'confidence': conf, 'why': why}


def confirmation(email):
    subj = email.get('subject') or ''
    m = re.search(r'\(([A-Z0-9]{6})\)', subj)
    if m and m.group(1) not in CODE_STOP and not re.fullmatch(r'[A-Z]{3}', m.group(1)):
        return m.group(1)
    for m in CONF.finditer(_all(email)):
        code = m.group(1).upper()
        if code in CODE_STOP or code.isdigit() and len(code) < 6:
            continue
        if not (m.group(1).isupper() or any(ch.isdigit() for ch in m.group(1))):
            continue  # "Reservation confirmed" is a word, not a code
        if re.fullmatch(r'[A-Z]+', code) and code.lower() in _all(email).lower().split() and m.group(1) != code:
            continue
        return code
    return None


# ------------------------------------------------------------------------------------------------ extract
def extract(email: dict, kind: str) -> dict:
    ref = _ref(email)
    fn = {'flight': flight, 'rx': rx, 'invite': invite, 'bill': bill, 'hotel': hotel}.get(kind)
    ev = fn(email, ref) if fn else {'type': kind, 'summary': email.get('subject')}
    ev.setdefault('type', kind)
    ev['source'] = {k: email.get(k) for k in ('message_id', 'mailbox', 'from', 'subject', 'date') if email.get(k)}
    return ev


def _ref(email):
    raw = email.get('date')
    try:
        d = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=OWNER_TZ)
    except (TypeError, ValueError):
        return datetime.now(OWNER_TZ)


def _at(day: datetime, hm, tz):
    return day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0, tzinfo=tz or OWNER_TZ)


def _carrier(email):
    hit = _matches(_domain(email.get('from')), AIRLINES)
    if hit:
        return AIRLINES[hit]
    text = _all(email).lower()
    best = None
    for n, c, k in CARRIER_NAMES:
        i = text.find(n)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, c, k)
    return (best[1], best[2]) if best else (None, None)


def flight(email, ref):
    body = _body(email)
    text = _all(email)
    carrier, code = _carrier(email)
    status = 'booked'
    if re.search(r'(?:has been|was|is|were|been) cancel(?:l)?ed|cancellation confirm|we(?:\'ve| have) cancel(?:l)?ed|'
                 r'^\s*cancel(?:l)?ed\b|reservaci[oó]n cancelada|vuelo cancelado|trip (?:is )?cancel', text, re.I | re.M):
        status = 'cancelled'
    elif re.search(r'schedule (?:has )?changed|schedule change|itinerary (?:has )?changed|(?:flight|time) (?:has )?changed|'
                   r'made a change to your|has been changed|updated (?:departing|returning|itinerary)|cambio de horario', text, re.I):
        status = 'changed'
    ev = {'type': 'flight', 'carrier': carrier, 'confirmation': confirmation(email), 'travelers': travelers(email.get('body_text') or ''),
          'segments': segments(body, ref, code), 'status': status}
    deps = [s['depart_local'] for s in ev['segments'] if s.get('depart_local')]
    for s in ev['segments']:
        if s.get('depart_local'):
            s['checkin_opens'] = (datetime.fromisoformat(s['depart_local']) - timedelta(hours=24)).isoformat()
    if deps:
        ev['checkin_opens'] = (datetime.fromisoformat(deps[0]) - timedelta(hours=24)).isoformat()
    return ev


def travelers(body):
    out = []
    for m in re.finditer(r'(?:passengers?|travel(?:l)?ers?|pasajeros?|guest)\s*(?::\s*|\n\s*)([^\n]+)', body, re.I):
        for part in re.split(r',|;| and | y ', m.group(1)):
            words = re.findall(NAME_WORD, part.split('  ')[0])
            words = [w for w in words if not re.match(r'(?i)aadvantage|skymiles|rapid|rewards|mileage|#', w)]
            if 2 <= len(words) <= 4 and not any(ch.isdigit() for ch in part.split('  ')[0]):
                name = ' '.join(w.capitalize() if w.isupper() or w.islower() else w for w in words)
                if name not in out:
                    out.append(name)
    return out


AIRPORT_TOK = re.compile(r'\(([A-Z]{3})\)|\b([A-Z]{3})\b')
FLIGHT_TOK = re.compile(r'(?:\bflight\s*#?\s*|\bvuelo\s*#?\s*|\b(WN|DL|AA|UA|AS|B6|NK|F9|HA|AM|Y4|G4|DELTA|SOUTHWEST|UNITED|ALASKA)\s*#?\s*)(\d{1,4})\b', re.I)
ARROW = re.compile(r'->|→|–>|\bto\b|\s-\s|—|–|\ba\b')


def segments(body, ref, code):
    """Walk the itinerary in reading order: each pair of airports (in parentheses, or a bare code next to a time
    or arrow) is one leg. Date = the latest date before the leg; times = the first time after each airport;
    flight number = the latest one before the leg; time zone = the airport's own."""
    toks = []
    for m in AIRPORT_TOK.finditer(body):
        c = m.group(1) or m.group(2)
        if m.group(2):
            if c not in TZ:
                continue
            after, before = body[m.end():m.end() + 6], body[max(0, m.start() - 5):m.start()]
            if not (TIME.match(after.strip()) or re.match(r'\s*(->|→|-|–|to\b)', after) or re.search(r'(->|→|–|-)\s*$', before)):
                continue
        toks.append((m.start(), m.end(), c))
    dates = dates_in(body, ref)
    times = times_in(body)
    # Real HTML mail repeats airports in headers and seat maps; when some airports carry a time, use only those.
    timed = [t for t in toks if any(t[1] <= x[1] < t[1] + 12 for x in times)]
    if len(timed) >= 2:
        toks = timed
    flights = list(FLIGHT_TOK.finditer(body))
    used_times = set()
    legs = []
    i = 0
    while i + 1 < len(toks):
        a, b = toks[i], toks[i + 1]
        gap = body[a[1]:b[0]]
        if a[2] == b[2] or gap.count('\n') > 8:
            i += 1
            continue

        def time_after(tok, limit):
            for t in times:
                if tok[1] <= t[1] < min(limit, tok[1] + 40) and t[1] not in used_times:
                    used_times.add(t[1])
                    return t[0]
            return None
        nxt = toks[i + 2][0] if i + 2 < len(toks) else len(body)
        ta = time_after(a, b[0] if b[0] > a[1] else len(body))
        tb = time_after(b, nxt)
        prev_end = legs[-1]['_end'] if legs else 0
        ds = [d for d in dates if d[1] < a[0] and d[1] >= prev_end - 200] or [d for d in dates if d[1] < a[0]]
        day = ds[-1][0] if ds else None
        line_start = body.rfind('\n', 0, a[0]) + 1
        fl = [f for f in flights if max(prev_end, 0) <= f.start() < b[0] and (f.start() >= line_start - 200)]
        num = None
        if fl:
            f = fl[-1]
            pre = (f.group(1) or '').upper()
            pre = {'DELTA': 'DL', 'SOUTHWEST': 'WN', 'UNITED': 'UA', 'ALASKA': 'AS'}.get(pre, pre)
            num = (pre if pre in CODES else (code or '')) + f.group(2)
        tza, tzb = TZ.get(a[2], OWNER_TZ), TZ.get(b[2], OWNER_TZ)
        dep = _at(day, ta, tza) if day and ta else None
        arr = _at(day, tb, tzb) if day and tb else None
        if dep and arr and arr < dep:
            arr = arr + timedelta(days=1)
        legs.append({'flight_no': num, 'from': a[2], 'to': b[2], 'depart_local': dep.isoformat() if dep else None,
                     'arrive_local': arr.isoformat() if arr else None, '_end': b[1]})
        i += 2
    for leg in legs:
        leg.pop('_end')
    return legs


def rx(email, ref):
    body = _body(email)
    text = _all(email)
    hit = _matches(_domain(email.get('from')), PHARMACIES)
    pharmacy = PHARMACIES[hit] if hit else None
    if not pharmacy:
        m = re.search(r'\b((?:[A-Z][a-z]+\s+){0,3}Pharmacy)\b', text)
        pharmacy = m.group(1).strip() if m else None
    if re.search(r'ready for pick ?up|prescription (?:is )?ready|order (?:is )?ready', text, re.I):
        status = 'ready'
    elif re.search(r'shipped|on (?:its|the) way', text, re.I):
        status = 'shipped'
    elif re.search(r'refill reminder|reminder', text, re.I) and not re.search(r'due for (?:a )?refill|refill is due|your refill is due', text, re.I):
        status = 'refill_reminder'
    elif re.search(r'due for (?:a )?refill|refill (?:is )?due|time to refill|next refill', text, re.I):
        status = 'refill_due'
    else:
        status = 'refill_reminder'
    drug = None
    m = re.search(r'(?:medication|medicine|drug)\s*:\s*([^\n]+)', body, re.I)
    if m:
        drug = m.group(1).strip()
    else:
        m = re.search(r'^\s*([A-Z][A-Z\*]{3,}(?:\s+[A-Z\*]+)*\s+\d+(?:\.\d+)?\s?MG(?:\s+[A-Z]{2,10})?)\s*$', body, re.M)
        if m:
            drug = m.group(1).strip()
    due = _date_after(text, ref, r'due date|refill due|due on|due|pick up by|pickup by|by') or (dates_in(text, ref) or [(None,)])[0][0]
    store = None
    m = re.search(r'(?:pick ?up at|your store|stop by|store|location)\s*:\s*\n?\s*([^\n]+)\n\s*([^\n]+)', body, re.I)
    if m:
        store = f"{m.group(1).strip()}, {m.group(2).strip()}"
    rxno = re.search(r'\bRx\s*#?\s*:?\s*([0-9\*]{4,})', text, re.I)
    return {'type': 'rx', 'pharmacy': pharmacy, 'drug': drug, 'status': status, 'due_date': due.date().isoformat() if due else None,
            'store': store, 'rx_number': rxno.group(1) if rxno else None}


def _ics(body):
    if 'BEGIN:VCALENDAR' not in body:
        return {}
    out = {}
    for line in body.splitlines():
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        name, _, params = k.partition(';')
        out.setdefault(name.upper(), (params, v.strip()))
    return out


def _ics_time(params, v):
    tzm = re.search(r'TZID=([^;:]+)', params or '')
    tz = ZoneInfo(tzm.group(1)) if tzm else (ZoneInfo('UTC') if v.endswith('Z') else OWNER_TZ)
    d = datetime.strptime(v.rstrip('Z')[:15], '%Y%m%dT%H%M%S')
    return d.replace(tzinfo=tz).isoformat(), (tzm.group(1) if tzm else None)


def invite(email, ref):
    subj = email.get('subject') or ''
    raw = email.get('body_text') or ''
    text = _all(email)
    ics = _ics(raw)
    method = 'request'
    if (ics.get('METHOD') or ('', ''))[1].upper() == 'CANCEL' or re.search(r'canceled event|cancelled event|has been cancel', text, re.I):
        method = 'cancel'
    elif re.search(r'updated invitation|invitation updated|has been (?:updated|changed)|changed:', text, re.I) or \
            int((ics.get('SEQUENCE') or ('', '0'))[1] or 0) > 0:
        method = 'update'
    title = (ics.get('SUMMARY') or (None, None))[1]
    if not title:
        title = re.sub(r'^\s*(updated invitation|invitation|canceled event|cancelled event)( with note)?:\s*', '', subj, flags=re.I)
        title = re.sub(r'\s*@\s.*$', '', title).strip() or None
    org = None
    if ics.get('ORGANIZER'):
        mm = re.search(r'mailto:([^\s;]+)', ics['ORGANIZER'][1] + ' ' + ics['ORGANIZER'][0], re.I)
        org = mm.group(1) if mm else None
    if not org:
        mm = re.search(r'organi[sz]er\s*:?\s*([\w.+-]+@[\w-]+\.[\w.]+)', text, re.I) or re.search(r'^([^\n]+?) has invited you', text, re.I | re.M)
        org = mm.group(1).strip() if mm else None
    uid = (ics.get('UID') or (None, None))[1] or (email.get('headers') or {}).get('X-Calendar-UID')
    guests = []
    gm = re.search(r'guests?\s*:\s*\n((?:[^\n]*@[^\n]*\n?)+)', raw, re.I)
    for e in re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', gm.group(1) if gm else raw):
        if e != uid and e not in guests and not e.endswith('google.com') and e not in CALENDAR_SENDERS:
            guests.append(e)
    start = end = tz = None
    if ics.get('DTSTART'):
        start, tz = _ics_time(*ics['DTSTART'])
        if ics.get('DTEND'):
            end, _ = _ics_time(*ics['DTEND'])
    else:
        ds = dates_in(text, ref); ts = times_in(text)
        if ds:
            tt = [t for t in ts if t[1] >= ds[0][1] - 40]
            if tt:
                start = _at(ds[0][0], tt[0][0], OWNER_TZ).isoformat()
                if len(tt) > 1:
                    end = _at(ds[0][0], tt[1][0], OWNER_TZ).isoformat()
        if re.search(r'\((?:PST|PDT)\)|Pacific Time', text):
            tz = 'America/Los_Angeles'
    return {'type': 'invite', 'organizer': org, 'title': title, 'start': start, 'end': end, 'timezone': tz,
            'attendees': guests, 'method': method, 'uid': uid}


def bill(email, ref):
    text = _all(email)
    status = 'failed' if re.search(r"failed|declined|unsuccessful|couldn'?t process", text, re.I) else \
        'overdue' if re.search(r'past due|overdue', text, re.I) else 'due'
    amt = re.search(r'(?:amount(?: due)?|total due|balance due|amount)\s*:?\s*\$\s?([0-9][0-9,]*(?:\.\d{2})?)', text, re.I) or MONEY.search(text)
    due = _date_after(text, ref, r'due date|due on|due by|pay by|update your payment method by|by|before') or \
        (dates_in(text, ref) or [(None,)])[0][0]
    name = parseaddr(email.get('from') or '')[0] or None
    if not name:
        m = re.search(r'^\s*([A-Z][\w&\']+(?:\s+[A-Z][\w&\']+){0,4})\s*\|', email.get('body_text') or '', re.M) or \
            re.search(r'\bThe ([A-Z][\w&\']+(?:\s+[A-Z][\w&\']+){0,3}) Team\b', text) or \
            re.search(r'\byour ([A-Z][\w&\']+)\s+(?:[A-Z]\w+\s+)?(?:plan|subscription|account)\b', text)
        name = m.group(1) if m else _domain(email.get('from')).split('.')[0] or None
    return {'type': 'bill', 'payee': name, 'amount': float(amt.group(1).replace(',', '')) if amt else None,
            'due_date': due.date().isoformat() if due else None, 'status': status}


def hotel(email, ref):
    text = _all(email)
    conf = confirmation(email)
    m = re.search(r'(?:booking with|stay(?:ing)? at|welcome to|reservation at)\s+(?:the\s+)?([A-Z][\w\'&]+(?:\s+[A-Z][\w\'&]+){0,5})', text)
    prop = m.group(1).strip() if m else (parseaddr(email.get('from') or '')[0] or None)
    g = re.search(r'\bguest(?: name)?\s*:\s*([^\n]+)', email.get('body_text') or '', re.I)
    ci = _date_after(text, ref, r'check-?in|arrival')
    co = _date_after(text, ref, r'check-?out|departure')
    return {'type': 'hotel', 'property': prop, 'confirmation': conf, 'guest': g.group(1).strip() if g else None,
            'check_in': ci.date().isoformat() if ci else None, 'check_out': co.date().isoformat() if co else None}


# ------------------------------------------------------------------------------------------- identity
def event_key(ev: dict) -> str | None:
    t = ev.get('type')
    if t == 'flight' and ev.get('confirmation'):
        return f"flight:{(ev.get('carrier') or '?').lower()}:{ev['confirmation']}"
    if t == 'invite':
        if ev.get('uid'):
            return f"invite:{ev['uid']}"
        if ev.get('title'):
            return f"invite:{ev['title'].lower()}:{(ev.get('organizer') or '').lower()}"
    if t == 'rx':
        return f"rx:{(ev.get('pharmacy') or '?').lower()}:{(ev.get('rx_number') or ev.get('drug') or '?').lower()}"
    if t == 'hotel' and ev.get('confirmation'):
        return f"hotel:{ev['confirmation']}"
    if t == 'bill':
        return f"bill:{(ev.get('payee') or '?').lower()}:{ev.get('due_date') or ev.get('amount') or '?'}"
    return None


def process(email: dict) -> dict:
    """One email -> {class, event_type, confidence, why, event?, key?}."""
    email = {**email, 'body_text': html_to_text(email.get('body_text') or '')}
    c = classify(email)
    if c['class'] == 'actionable_automated':
        ev = extract(email, c['event_type'])
        c['event'] = ev
        c['key'] = event_key(ev)
    return c
