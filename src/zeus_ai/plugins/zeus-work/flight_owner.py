"""Read the existing trip owner's identity. This module never reads a flight source or sends a text.
The trip soft sleep below reads flights.json only; its optional quiet check sends a short no-names reading to a
configured classifier endpoint when its own switch is on and the endpoint is configured."""
import datetime as _dt
import json
import os
from pathlib import Path
import re
import urllib.request

from .store import digest

import sys as _sys
_ENGINE = Path(__file__).resolve().parents[2] / 'engine'
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
import settings  # noqa: E402


def registered_trip(home, trip_id):
    if not isinstance(trip_id, str) or not re.fullmatch(r't-[a-f0-9]{8}', trip_id):
        return None
    life = Path(home) / 'workspace/life-engine'
    if not (life / 'research-flow-on').exists():
        return None
    try:
        trips = json.loads((life / 'flights.json').read_text())['trips']
    except (OSError, TypeError, ValueError, KeyError):
        return None
    matches = [trip for trip in trips if isinstance(trip, dict) and trip.get('id') == trip_id]
    if len(matches) != 1:
        return None
    trip = matches[0]
    legs = trip.get('legs') or []
    if not legs or any(not all(leg.get(k) for k in ('ident','date','origin','dest')) for leg in legs):
        return None
    identity = [(leg['date'], leg['ident'], leg['origin'], leg['dest']) for leg in legs]
    itinerary = {'legs': identity, 'who': trip.get('who'), 'own_trip': trip.get('own_trip'), 'asked': sorted(trip.get('asked') or []), 'airport': (trip.get('pickup') or {}).get('airport')}
    return {'id': trip_id, 'itinerary_hash': digest(itinerary), 'state': trip.get('state')}


def verified_watch_ids(store, record):
    """Read a previously checked artifact back at its canonically owned path.
    The lookup is narrow and used once when binding, never on each timer tick.
    """
    ids = set()
    ref = record.get('external_ref') or {}
    if ref.get('system') == 'life-flight' and isinstance(ref.get('id'), str):
        ids.add(ref['id'])
    seen = record.get('evidence') or {}
    for criterion in record.get('criteria') or []:
        proof = seen.get(criterion['id']) or {}
        if not proof.get('passed') or proof.get('revision') != record['revision'] or not proof.get('sha256'):
            continue
        path = store.artifact_path(record['id'], criterion['artifact'])
        try:
            if not path.is_file() or path.stat().st_size > 4_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if digest(data) != proof['sha256']:
            continue
        pattern = rb'(?i)(?:already watching that trip|watching|native (?:flight )?watch)\s*\(?\s*(t-[a-f0-9]{8})(?![a-z0-9-])'
        ids.update(match.decode().lower() for match in re.findall(pattern, data))
    return ids


def same_work_trip(home, record):
    """Only accept an explicit trip binding. A name, flight number or watch on another task cannot claim this one."""
    reference = record.get('external_ref') or {}
    binding = record.get('flight_monitor')
    if binding:
        if binding.get('revision') != record['revision'] or binding.get('scope_hash') != record['scope_hash']:
            return None
        pending = {step['id'] for step in record['steps'] if step['state'] != 'checked'}
        if not pending or not pending.issubset(binding['steps']):
            return None
        trip = registered_trip(home, binding['id'])
        if trip and trip['itinerary_hash'] == binding['itinerary_hash']:
            return trip
    if reference.get('system') == 'life-flight':
        return registered_trip(home, reference.get('id'))
    return None


# ---------------------------------------------------------------------------
# Trip soft sleep for UNBOUND flight/pickup work items.
# 
# Why: a work item created before `life flight add` registered its trip is never bound to that trip, so
# sleeping_reason() never applies and the item would wake the model every minute while the trip watcher (which
# sends the layover, landing and leave-by texts itself) watches the same flights.
# 
# What this does (switch `plugin-data/zeus-work/trip-soft-sleep-on`):
# - Only for an OPEN item with no trip binding whose ORIGINAL request text clearly points at exactly one
#   trip in state `watching` on the same day. "Clearly points" = the text names every leg ident of that
#   trip (UA576, UA 576), or the words the trip watcher saved from the same request (`trip.words`, at
#   least 40 characters) appear in the request text and it names the trip's traveller. Anything else returns None: the original gate runs.
# - Skips the wake unless (1) the trip's structured fingerprint changed since this item's last ADMITTED
#   wake, (2) 30 minutes passed since that wake, or (3) the trip it was matched to ended or vanished.
# - Never claims steps and never changes the binding. The fingerprint is stored only on admitted wakes.
# 
# Optional quiet check (switch `plugin-data/zeus-work/jev-quiet-on`, needs the first switch, and needs
# `flights.quiet_check_endpoint` configured): only when the fingerprint changed (not the 30-minute or
# trip-ended wakes), a configured classifier is asked whether the change matters. Quiet only at p <= 0.10.
# A new line containing a force-wake word always wakes. Any error (including no endpoint or no key) wakes.
# 
# Every failure inside this module returns None (= the original behaviour: wake).
SOFT_SWITCH = 'trip-soft-sleep-on'
JEV_SWITCH = 'jev-quiet-on'     # switch-file name kept for compatibility; it turns on the optional quiet check
FORCE_AFTER = 30 * 60          # seconds since the last admitted wake
MIN_WORDS = 40                 # trip.words shorter than this never matches
READING_CHARS = 600
JEV_QUIET_P = 0.10
JEV_TIMEOUT = 3          # seconds; the gate holds the work database while it waits
# Optional classifier: POST {'state','model','questions'} with a bearer key; answer answers.q.noul in [0, 1].
JEV_ENDPOINT = str(settings.get('flights.quiet_check_endpoint', '') or '')
JEV_MODEL = str(settings.get('flights.quiet_check_model', '') or '')
JEV_KEY_ENV = str(settings.get('flights.quiet_check_key_env', 'ZEUS_QUIET_CHECK_KEY') or 'ZEUS_QUIET_CHECK_KEY')
JEV_KEY_FILE = Path(str(settings.get('flights.quiet_check_key_file', '') or '')) if settings.get('flights.quiet_check_key_file') else None
FORCE_WORDS = ('degraded', 'could not be read', 'cancel', 'landed', 'arrived', 'departed', 'diverted', 'delay')
TZ = settings.OWNER_TZ

JEV_CONTEXT = ("A personal assistant is watching someone's flights and the airport pickup for that person, "
               "and texts the owner when something changes.")
JEV_QUESTION = {
    'type': 'noul',
    'instructions': ('Compare BEFORE and AFTER. Answer true if AFTER contains ANY new fact versus BEFORE that could change what the assistant should '
                     'do or say next: a new flight state or event (departed, delayed, landed, gate, ETA change, data could not be read, coverage degraded), an alert '
                     'marked sent or due, a pickup or leave-by time now close or past, a new inbox message, a failed check, or the watched thing finishing. '
                     'Answer false only if AFTER repeats the same facts with nothing new except check times, next-read times, ids or hashes.'),
    'criteria': {'true': 'Any new fact that could change the next action or message, including errors and approaching deadlines.',
                 'false': 'Only the same facts repeated; differences are just timestamps, next-check times, ids or hashes.'},
}


def switch_on(home, name):
    return (Path(home) / 'plugin-data' / 'zeus-work' / name).exists()


def _local_day(ts):
    from zoneinfo import ZoneInfo
    return _dt.datetime.fromtimestamp(float(ts), ZoneInfo(TZ)).date()


def load_trips(home):
    data = json.loads((Path(home) / 'workspace/life-engine/flights.json').read_text())
    trips = data.get('trips')
    if not isinstance(trips, list):
        raise ValueError('flights.json has no trip list')
    return [t for t in trips if isinstance(t, dict)]


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def request_text(record):
    """The original request plus any user correction. Never agent-written checkpoints."""
    texts = [str((record.get('source') or {}).get('text') or '')]
    for rev in record.get('revisions') or []:
        texts.append(str(((rev or {}).get('source') or {}).get('text') or ''))
    return '\n'.join(texts)


def _names_ident(text, leg):
    carrier, number = str(leg.get('carrier') or ''), str(leg.get('number') or '')
    if not (carrier and number):
        m = re.fullmatch(r'([A-Z0-9]{2})(\d{1,4})', str(leg.get('ident') or '').upper())
        if not m:
            return False
        carrier, number = m.groups()
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(carrier) + r'\s?-?' + re.escape(number) + r'(?!\d)', text, re.I) is not None


def match_rule(record, trip):
    """'idents' / 'words' when the request text points at this trip, else None."""
    legs = trip.get('legs') or []
    if not legs or any(not (leg.get('ident') and leg.get('date')) for leg in legs):
        return None
    text = request_text(record)
    if all(_names_ident(text, leg) for leg in legs):
        return 'idents'
    words = _norm(trip.get('words'))
    who = str(trip.get('who') or '').strip()
    # Same saved words AND the same person: two friends asked for with the same phrasing never cross-match.
    if len(words) >= MIN_WORDS and words in _norm(text) and (not who or re.search(r'(?<!\w)' + re.escape(who) + r'(?!\w)', text, re.I)):
        return 'words'
    return None


def same_day(record, trip, now_ts):
    days = sorted({_dt.date.fromisoformat(str(leg['date'])[:10]) for leg in trip.get('legs') or []})
    if not days:
        return False
    created = _local_day(record.get('created_at') or 0)
    today = _local_day(now_ts)
    return days[0] <= created <= days[-1] and days[0] <= today <= days[-1] + _dt.timedelta(days=1)


def unbound(record):
    return not record.get('flight_monitor') and not ((record.get('external_ref') or {}).get('system') == 'life-flight')


def match(record, trips, now_ts):
    """The one watching, same-day trip the request points at, or None (also None when two match)."""
    found = []
    for trip in trips:
        if trip.get('state') != 'watching' or not same_day(record, trip, now_ts):
            continue
        rule = match_rule(record, trip)
        if rule:
            found.append((trip, rule))
    return found[0] if len(found) == 1 else None


def fingerprint(trip):
    """Structured facts only: leg facts/seen/told/closed, the alert keys the watcher already sent, the
    leave-by it sent, and whether coverage is degraded. Check times, next-read times and notes are left out."""
    legs = [{k: leg.get(k) for k in ('ident', 'date', 'origin', 'dest', 'facts', 'seen', 'told', 'closed')}
            for leg in trip.get('legs') or []]
    leave = ((trip.get('pickup') or {}).get('leave') or {})
    return digest({'id': trip.get('id'), 'state': trip.get('state'), 'legs': legs,
                   'sent': sorted(str(x) for x in trip.get('fingerprints') or []),
                   'leave': [leave.get('leave_by'), leave.get('notified_at')],
                   'degraded': int(trip.get('failures') or 0) >= 3})


def reading(trip):
    """Short plain reading for the quiet check. No names, no addresses. One line per leg."""
    lines = []
    for leg in trip.get('legs') or []:
        f = leg.get('facts') or {}
        d = leg.get('delay') or {}
        told = ', '.join(sorted(str(k) for k in (leg.get('told') or {})))
        lines.append(f"{leg.get('ident')} {leg.get('origin')}-{leg.get('dest')}: {f.get('status') or 'unknown'}; "
                     f"{f.get('status_text') or ''}; dep gate {f.get('dep_gate') or '-'} term {f.get('dep_terminal') or '-'}; "
                     f"arr gate {f.get('arr_gate') or '-'} term {f.get('arr_terminal') or '-'}; late {d.get('minutes') or 0} min"
                     + (f'; already texted about {told}' if told else ''))
    sent = [str(x) for x in trip.get('fingerprints') or []]
    lines.append(f"alerts sent so far: {len(sent)}" + (f" (latest {re.sub(r'^t-[a-f0-9]{8}-', 'pickup-', sent[-1])})" if sent else ''))
    if (((trip.get('pickup') or {}).get('leave') or {}).get('notified_at')):
        lines.append('leave-by text already sent')
    if int(trip.get('failures') or 0) >= 3:
        lines.append('flight data could not be read 3 times in a row; coverage is degraded')
    return '\n'.join(lines)[:READING_CHARS]


def force_word(before, after):
    """A force-wake word on a line that is new in AFTER. Old lines repeated unchanged are not news."""
    old = set((before or '').splitlines())
    new = [ln.lower() for ln in (after or '').splitlines() if ln not in old]
    for w in FORCE_WORDS:
        if any(w in ln for ln in new):
            return w
    return None


def _api_key():
    k = os.environ.get(JEV_KEY_ENV, '').strip()
    if k:
        return k
    if JEV_KEY_FILE is None:
        return None
    try:
        return JEV_KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def jev_state(before, after, check_ts):
    from zoneinfo import ZoneInfo
    t = _dt.datetime.fromtimestamp(float(check_ts), ZoneInfo(TZ)).strftime('%a %b %d %I:%M %p %Z')
    return f"{JEV_CONTEXT}\nCheck time: {t}\nBEFORE: {(before or '')[:READING_CHARS]}\nAFTER: {(after or '')[:READING_CHARS]}"


def jev_ask(state, timeout=JEV_TIMEOUT):
    if not JEV_ENDPOINT:
        raise RuntimeError('no quiet-check endpoint configured')
    key = _api_key()
    if not key:
        raise RuntimeError('no quiet-check key')
    body = json.dumps({'state': state, 'model': JEV_MODEL, 'questions': {'q': JEV_QUESTION}}).encode()
    req = urllib.request.Request(JEV_ENDPOINT, data=body, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ans = json.loads(r.read().decode())
    p = float(ans['answers']['q']['noul'])
    if not 0.0 <= p <= 1.0:
        raise ValueError('noul out of range')
    return p


def jev_quiet(before, after, check_ts, ask=None):
    """(quiet, detail). Quiet only when no force word is new and the check says p <= 0.10. Any error is not quiet."""
    w = force_word(before, after)
    if w:
        return False, {'jev': 'not asked', 'force_word': w}
    try:
        p = (ask or jev_ask)(jev_state(before, after, check_ts))
        p = float(p)
        if not 0.0 <= p <= 1.0:
            raise ValueError('noul out of range')
    except Exception as exc:                      # any failure wakes
        return False, {'jev': 'error', 'error_type': type(exc).__name__}
    return p <= JEV_QUIET_P, {'jev': 'asked', 'p': round(p, 3)}


def decide(home, record, now_ts, trips=None, ask=None, last_skip=None):
    """None = not applicable (switch off, bound, not open, no single matching trip, any error): the
    original gate runs unchanged and nothing is stored. Otherwise a dict with
    skip (bool), why, and mark (what an admitted wake stores in record['trip_soft']).
    last_skip: payload of this item's most recent trip_soft_skip event (or None). A quiet-check verdict for the
    SAME before/after fingerprints is reused instead of asking the quiet check again every minute."""
    try:
        if not switch_on(home, SOFT_SWITCH):
            return None
        if record.get('state') != 'open' or not unbound(record):
            return None
        trips = load_trips(home) if trips is None else trips
        prev = record.get('trip_soft') or {}
        found = match(record, trips, now_ts)
        if not found:
            if prev.get('trip_id'):
                # The trip it was sleeping on ended or vanished: wake once and stop sleeping.
                return {'skip': False, 'why': 'trip_ended', 'mark': dict(prev, ended_at=now_ts, at=now_ts)} if not prev.get('ended_at') else None
            return None
        trip, rule = found
        fp, now_reading = fingerprint(trip), reading(trip)
        mark = {'trip_id': trip['id'], 'rule': rule, 'fp': fp, 'reading': now_reading, 'at': now_ts}
        if prev.get('trip_id') != trip['id'] or not prev.get('fp'):
            return {'skip': False, 'why': 'first_match', 'mark': mark}
        if now_ts - float(prev.get('at') or 0) >= FORCE_AFTER:
            return {'skip': False, 'why': 'forced_30_min', 'mark': mark}
        if fp == prev['fp']:
            return {'skip': True, 'why': 'trip_unchanged', 'event': {'trip_id': trip['id'], 'rule': rule, 'fp': fp[:16], 'from_fp': prev['fp'][:16],
                                                                     'since_admitted_s': round(now_ts - float(prev['at']))}}
        if switch_on(home, JEV_SWITCH):
            ls = last_skip or {}
            if ls.get('why') == 'jev_quiet' and ls.get('from_fp') == prev['fp'][:16] and ls.get('fp') == fp[:16]:
                return {'skip': True, 'why': 'jev_quiet', 'event': {'trip_id': trip['id'], 'rule': rule, 'fp': fp[:16], 'from_fp': prev['fp'][:16],
                                                                    'jev': 'reused', 'p': ls.get('p'), 'since_admitted_s': round(now_ts - float(prev['at']))}}
            quiet, detail = jev_quiet(prev.get('reading'), now_reading, now_ts, ask=ask)
            if quiet:
                return {'skip': True, 'why': 'jev_quiet', 'event': {'trip_id': trip['id'], 'rule': rule, 'fp': fp[:16], 'from_fp': prev['fp'][:16], **detail,
                                                                    'since_admitted_s': round(now_ts - float(prev['at']))}}
            return {'skip': False, 'why': 'trip_changed', 'mark': mark, 'jev': detail}
        return {'skip': False, 'why': 'trip_changed', 'mark': mark}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Trip auto-bind (switch `plugin-data/zeus-work/trip-auto-bind-on`).
#
# Why: the base binding rule only trusts a trip ID that Zeus wrote into a checked artifact. When the work item
# is made before `life flight add`, that never happens, so the item would stay unbound and wake for nothing.
# What this adds:
# - matched_trip_id(): the one watching, same-day trip the ORIGINAL request text points at (the same strict
#   rule as the soft sleep: every leg's flight number, or the trip watcher's saved request words plus the
#   traveller's name). Code checks it; a model guess never counts.
# - bind_trip() accepts that match as proof of the trip ID. Zeus still names which steps the watcher owns.
# - bind_hint(): one line added to the wake context of an unbound open item that matches, telling Zeus the
#   trip ID and to hand the watcher its steps. After binding, the item sleeps until the trip ends.
# Any error returns None (= the original behaviour).
AUTOBIND_SWITCH = 'trip-auto-bind-on'


def matched_trip_id(home, record, now_ts):
    try:
        if not switch_on(home, AUTOBIND_SWITCH) or record.get('state') != 'open' or not unbound(record):
            return None
        found = match(record, load_trips(home), now_ts)
        return found[0]['id'] if found else None
    except Exception:
        return None


def bind_hint(home, record, now_ts):
    trip_id = matched_trip_id(home, record, now_ts)
    if not trip_id:
        return None
    pending = [s['id'] for s in record.get('steps') or [] if s.get('state') != 'checked']
    return (f"Trip check: this work matches the native trip watch {trip_id} (code-checked from the request). "
            f"That watcher already sends the layover, landing and pickup texts. If any unchecked step "
            f"({', '.join(pending) or 'none'}) is only that tracking, call zeus_work schedule with "
            f"flight_watch_id={trip_id} and flight_watch_steps listing just those steps; this work then sleeps "
            f"until the trip ends. Keep any step that is separate work.")
