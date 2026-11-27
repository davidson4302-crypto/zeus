"""Model step for life intake. The model reads the email and fills a strict form; code then checks every
value against the email text and attaches time zones, so nothing it invents survives.

Runs `claude -p` lean (no settings, tools, MCP or memory; ~$0.0006 a call on Haiku 5.5).
If the model is unavailable, callers fall back to the rule-based extractor.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import life_intake as L
import settings  # life_intake puts engine/ on sys.path

MODEL = os.environ.get('LIFE_INTAKE_MODEL', 'claude-haiku-5-5')
CLAUDE = os.environ.get('CLAUDE_BIN', settings.CLAUDE_BIN)
EMPTY_MCP = Path(__file__).with_name('empty-mcp.json')

SEG = {'type': 'object', 'properties': {
    'flight_no': {'type': ['string', 'null'], 'description': 'carrier code + number, e.g. UA233, WN1423'},
    'from': {'type': 'string', 'description': '3-letter IATA'}, 'to': {'type': 'string'},
    'depart_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD local'}, 'depart_time': {'type': ['string', 'null'], 'description': 'HH:MM 24h local'},
    'arrive_date': {'type': ['string', 'null']}, 'arrive_time': {'type': ['string', 'null']}},
    'required': ['from', 'to', 'flight_no', 'depart_date', 'depart_time', 'arrive_date', 'arrive_time']}
SCHEMA = {'type': 'object', 'required': ['class', 'event_type'], 'properties': {
    'class': {'enum': ['actionable_automated', 'human', 'junk']},
    'event_type': {'enum': ['flight', 'rx', 'invite', 'bill', 'hotel', 'delivery', 'appointment', None]},
    'flight': {'type': 'object', 'properties': {
        'carrier': {'type': ['string', 'null']}, 'confirmation': {'type': ['string', 'null']},
        'travelers': {'type': 'array', 'items': {'type': 'string'}}, 'status': {'enum': ['booked', 'changed', 'cancelled']},
        'segments': {'type': 'array', 'items': SEG}}},
    'rx': {'type': 'object', 'properties': {k: {'type': ['string', 'null']} for k in ('pharmacy', 'drug', 'status', 'due_date', 'store', 'rx_number')}},
    'invite': {'type': 'object', 'properties': {
        'organizer': {'type': ['string', 'null']}, 'title': {'type': ['string', 'null']}, 'start': {'type': ['string', 'null']},
        'end': {'type': ['string', 'null']}, 'timezone': {'type': ['string', 'null']}, 'attendees': {'type': 'array', 'items': {'type': 'string'}},
        'method': {'enum': ['request', 'update', 'cancel']}, 'uid': {'type': ['string', 'null']}, 'note': {'type': ['string', 'null']}}},
    'bill': {'type': 'object', 'properties': {'payee': {'type': ['string', 'null']}, 'amount': {'type': ['number', 'null']},
                                              'due_date': {'type': ['string', 'null']}, 'status': {'enum': ['due', 'failed', 'overdue']}}},
    'hotel': {'type': 'object', 'properties': {k: {'type': ['string', 'null']} for k in ('property', 'confirmation', 'guest', 'check_in', 'check_out')}},
}}

SYSTEM = """You read one email for a personal assistant and fill the JSON schema. Rules:
- class: actionable_automated = machine-sent mail that needs action or tracking (flight booking/change/cancel/check-in, boarding pass,
  hotel/rental confirmation, prescription ready/shipped/refill due/refill reminder, calendar invite/update/cancel, bill due or
  failed/declined payment, delivery needing a signature, appointment confirmation). A human forwarding such mail is still
  actionable_automated. human = a real person writing to the reader. junk = marketing, sales, promos (even naming routes, flights or
  drugs), newsletters, surveys, statements, receipts, autopay-scheduled notices, onboarding, account notices, and calendar
  "Notification:" reminders for events already on the calendar.
- Copy codes, names, drugs and numbers exactly as printed (keep masking like ***). Never guess a value; use null.
- Flights: one segment per flight leg in order, local date and 24h time at EACH airport as printed. flight_no = airline code +
  number (Southwest WN, Delta DL, American AA, United UA, Alaska AS, JetBlue B6, Spirit NK, Frontier F9, Hawaiian HA, Aeromexico AM,
  Volaris Y4, Allegiant G4). carrier = short name (Southwest, Delta, American, United, Alaska, JetBlue, Spirit, Frontier, Hawaiian,
  Aeromexico, Volaris, Allegiant). status changed for schedule/trip changes, cancelled only if THIS booking is cancelled (not policy text).
  Travelers as "First Last" in normal capitalization.
- Prescriptions: pharmacy = short brand (CVS, Walgreens, Rite Aid, Express Scripts, Amazon Pharmacy, or the printed store name);
  status ready | refill_due | refill_reminder | shipped; due_date YYYY-MM-DD (pick-up-by, due or refill date);
  store = "Name #number, street, city, ST zip" when printed.
- Invites: start/end as ISO with offset for the event time zone, timezone as IANA (e.g. America/New_York), organizer email,
  attendees emails, uid from the calendar data, method request|update|cancel, note = any note the organizer wrote.
- Bills: payee = the company or card name the bill is for, amount as a number, due_date YYYY-MM-DD (the date to pay or fix by).
- Dates: today is {today}; resolve dates without a year to the next future occurrence."""


def call_model(email: dict, timeout=90) -> dict | None:
    body = L.html_to_text(email.get('body_text') or '')[:9000]
    prompt = (f"From: {email.get('from')}\nTo: {email.get('to') or ''}\nSubject: {email.get('subject') or ''}\n"
              f"Date: {email.get('date') or ''}\nLabels: {', '.join(email.get('labels') or [])}\n\n{body}")
    today = (str(email.get('date') or '')[:10]) or datetime.now(L.OWNER_TZ).date().isoformat()
    if not EMPTY_MCP.exists():
        EMPTY_MCP.write_text('{"mcpServers":{}}')
    cmd = [CLAUDE, '-p', '--setting-sources', '', '--strict-mcp-config', '--mcp-config', str(EMPTY_MCP),
           '--disable-slash-commands', '--no-session-persistence', '--tools', '', '--exclude-dynamic-system-prompt-sections',
           '--system-prompt', SYSTEM.replace('{today}', today), '--model', MODEL, '--output-format', 'json',
           '--json-schema', json.dumps(SCHEMA), prompt]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, 'HOME': str(settings.HOME)}, cwd=tempfile.gettempdir())
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    if d.get('is_error') or not isinstance(d.get('structured_output'), dict):
        return None
    out = d['structured_output']
    out['_cost'] = d.get('total_cost_usd')
    return out


# ------------------------------------------------------------------------------------------- checks
def _in_text(value, text):
    if value in (None, ''):
        return False
    v = re.sub(r'\s+', ' ', str(value)).strip().lower()
    return v in re.sub(r'\s+', ' ', text).lower()


def _local(date, hm, airport):
    if not date or not hm:
        return None
    try:
        d = datetime.strptime(f'{date} {hm}', '%Y-%m-%d %H:%M')
    except ValueError:
        return None
    return d.replace(tzinfo=L.TZ.get(airport, L.OWNER_TZ))


def checked_event(email: dict, m: dict) -> dict | None:
    """Turn the model's form into an event, dropping anything not supported by the email text."""
    kind = m.get('event_type')
    text = L._all({**email, 'body_text': L.html_to_text(email.get('body_text') or '')})
    raw = m.get(kind) or {}
    if kind == 'flight':
        segs = []
        for s in raw.get('segments') or []:
            a, b = str(s.get('from') or '').upper(), str(s.get('to') or '').upper()
            if not (re.fullmatch(r'[A-Z]{3}', a) and re.fullmatch(r'[A-Z]{3}', b) and a in text and b in text):
                continue
            dep = _local(s.get('depart_date'), s.get('depart_time'), a)
            arr = _local(s.get('arrive_date') or s.get('depart_date'), s.get('arrive_time'), b)
            if dep and arr and arr < dep and not s.get('arrive_date'):
                arr += timedelta(days=1)
            fno = re.sub(r'\s+', '', str(s.get('flight_no') or '')).upper() or None
            if fno and not re.search(r'\b' + re.escape(re.sub(r'^[A-Z0-9]{2}', '', fno)) + r'\b', text):
                fno = None
            seg = {'flight_no': fno, 'from': a, 'to': b, 'depart_local': dep.isoformat() if dep else None,
                   'arrive_local': arr.isoformat() if arr else None}
            if dep:
                seg['checkin_opens'] = (dep - timedelta(hours=24)).isoformat()
            segs.append(seg)
        conf = raw.get('confirmation')
        conf = str(conf).upper() if conf and _in_text(conf, text) else L.confirmation(email)
        ev = {'type': 'flight', 'carrier': raw.get('carrier'), 'confirmation': conf,
              'travelers': [t for t in raw.get('travelers') or [] if all(_in_text(w, text) for w in t.split())],
              'segments': segs, 'status': raw.get('status') or 'booked'}
        deps = [s['checkin_opens'] for s in segs if s.get('checkin_opens')]
        if deps:
            ev['checkin_opens'] = min(deps)
        return ev
    if kind == 'rx':
        ev = {'type': 'rx', **{k: raw.get(k) for k in ('pharmacy', 'drug', 'status', 'due_date', 'store', 'rx_number')}}
        for k in ('drug', 'rx_number'):
            if ev.get(k) and not _in_text(ev[k], text):
                ev[k] = None
        return ev
    if kind == 'invite':
        ev = {'type': 'invite', **{k: raw.get(k) for k in ('organizer', 'title', 'start', 'end', 'timezone', 'attendees', 'method', 'uid', 'note')}}
        ev['attendees'] = [a for a in ev.get('attendees') or [] if _in_text(a, text)]
        if ev.get('uid') and not _in_text(ev['uid'], text):
            ev['uid'] = None
        return ev
    if kind == 'bill':
        return {'type': 'bill', **{k: raw.get(k) for k in ('payee', 'amount', 'due_date', 'status')}}
    if kind == 'hotel':
        ev = {'type': 'hotel', **{k: raw.get(k) for k in ('property', 'confirmation', 'guest', 'check_in', 'check_out')}}
        if ev.get('confirmation') and not _in_text(ev['confirmation'], text):
            ev['confirmation'] = None
        return ev
    return {'type': kind, 'summary': email.get('subject')}


def process(email: dict, use_model=True) -> dict:
    """Model first, rules as the fallback and as a guard for strong junk/calendar-reminder signals."""
    email = {**email, 'body_text': L.html_to_text(email.get('body_text') or '')}
    rules = L.process(email)
    if not use_model:
        rules['engine'] = 'rules'
        return rules
    m = call_model(email)
    if not m:
        rules['engine'] = 'rules (model unavailable)'
        return rules
    out = {'class': m.get('class'), 'event_type': m.get('event_type') if m.get('class') == 'actionable_automated' else None,
           'confidence': 0.9, 'why': 'model read the email', 'engine': MODEL, 'cost': m.get('_cost')}
    if out['class'] == 'actionable_automated' and out['event_type']:
        ev = checked_event(email, m)
        ev['source'] = {k: email.get(k) for k in ('message_id', 'mailbox', 'from', 'subject', 'date') if email.get(k)}
        out['event'] = ev
        out['key'] = L.event_key(ev)
    elif out['class'] == 'actionable_automated':
        out['class'] = rules['class']; out['event_type'] = rules.get('event_type')
        out['event'] = rules.get('event'); out['key'] = rules.get('key')
    return out
