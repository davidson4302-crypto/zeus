"""Noticer: one observation in -> zero or more opportunities out. General: nothing here is about flights or birthdays.

The model is asked what a world-class assistant would do. Code then checks the answer: the evidence quote must be
in the observation, classes must be known, and the policy (not the model) decides autonomy.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import settings

MODEL = os.environ.get('LIFE_NOTICER_MODEL') or settings.get('models.noticer_claude', 'claude-sonnet-5-5')
CLAUDE = os.environ.get('CLAUDE_BIN', settings.CLAUDE_BIN)
EMPTY_MCP = Path(__file__).with_name('empty-mcp.json')
CLASSES = ['none_needed', 'prepare_or_research', 'remind_owner', 'notify_owner_now', 'self_service_free',
           'spend_money', 'message_outside_person', 'calendar_change', 'delete_or_cancel']
OWNER = settings.OWNER_FIRST          # how the prompts name the owner (first name, or "the owner")


def _fill(text: str) -> str:
    """Prompt templates name the owner and the agent with plain tokens, so JSON-like braces need no escaping."""
    return (text.replace('{OWNER}', OWNER).replace('{AGENT}', settings.AGENT_NAME)
            .replace('{QUIET_END}', '%d:00' % settings.QUIET_END))


SCHEMA = {'type': 'object', 'required': ['opportunities'], 'properties': {'opportunities': {'type': 'array', 'items': {
    'type': 'object',
    'required': ['title', 'why', 'proposed_action', 'action_class', 'urgency', 'evidence', 'dedupe_key', 'confidence', 'steps', 'text', 'owner_involved'],
    'properties': {
        'title': {'type': 'string', 'description': 'short, plain'},
        'why': {'type': 'string', 'description': _fill('why it matters to {OWNER}, one sentence')},
        'proposed_action': {'type': 'string', 'description': 'exactly what the assistant would do or offer, plain words'},
        'action_class': {'enum': CLASSES},
        'urgency': {'enum': ['now', 'today', 'later']},
        'surface_at': {'type': ['string', 'null'], 'description': _fill('ISO time to tell {OWNER} or act; null = next brief')},
        'deadline': {'type': ['string', 'null'], 'description': 'ISO time after which this is moot'},
        'done_condition': {'type': ['string', 'null'], 'description': 'what proves it is finished'},
        'evidence': {'type': 'string', 'description': 'short exact quote from the observation that triggered this'},
        'dedupe_key': {'type': 'string', 'description': 'stable id of the real-world thing, e.g. flight:southwest:ABC123, birthday:sister:YYYY-MM-DD, bill:acme:YYYY-MM'},
        'confidence': {'type': 'number'},
        'details': {'type': ['object', 'null'], 'description': 'structured facts if useful (legs, amounts, dates, people)'},
        'text': {'type': 'string', 'description': _fill('the iMessage {AGENT} would send {OWNER}: first person, warm, casual, plain words, max 280 chars, no jargon, ends with one clear question or says it is done')},
        'owner_involved': {'enum': ['yes', 'no', 'unknown'], 'description': _fill('is {OWNER} personally affected (their calendar, their money, their family, their work)?')},
        'steps': {'type': 'array', 'description': 'every concrete step of the plan, in order, each with its own class',
                  'items': {'type': 'object', 'required': ['do', 'action_class'],
                            'properties': {'do': {'type': 'string'}, 'action_class': {'enum': CLASSES},
                                           'when': {'type': ['string', 'null']}}}},
    }}}}}

# Optional one or two sentences about the owner's life and work (config owner.about), so the model knows the context.
ABOUT = str(settings.get('owner.about', '') or '').strip()

SYSTEM = _fill("""You are {AGENT}, {OWNER}'s world-class personal assistant.""" + (' ' + ABOUT if ABOUT else '') + """
Your job: notice anything in {OWNER}'s email and calendar that a great human executive assistant would act on, prepare,
offer or watch, and solve problems before {OWNER} sees them. Think broadly: travel, health, money, deadlines, renewals,
expirations, family dates, conflicts, preparation for meetings, logistics, follow-ups, risks. Examples of the spirit
(not a list to match): check {OWNER} into a flight and send the boarding pass; refill or remind about a prescription;
text {OWNER} when a new invite clashes with a meeting with a business partner and suggest which to move and where; see
a family member's birthday and offer to set a reminder to call them.

Return opportunities ONLY when real value exists. Return an empty list for newsletters, promotions (even urgent-
sounding), completed receipts, FYIs with nothing to do, social notifications, phishing/spam, things already handled
(see open opportunities) and cancelled items already off the calendar. Never invent facts; quote evidence exactly.
action_class: none_needed | prepare_or_research | remind_owner | notify_owner_now | self_service_free (free,
reversible action in {OWNER}'s own account, e.g. airline check-in) | spend_money | message_outside_person |
calendar_change | delete_or_cancel. Pick the class of the main thing you'd DO. urgency now = {OWNER} must know or act
within hours; today; later. Nothing routine before quiet hours end ({QUIET_END} on {OWNER}'s clock): put surface_at at
or after {QUIET_END} local unless urgent.
Be concrete in proposed_action (who, what, when, which option you'd pick and why).

Think it through like a executive assistant before answering. For every real item, work through:
1. Consequences: what happens before, during and after this? (getting there, leave times, rides, time zones, jet lag,
   what it collides with in nearby_calendar, who else is affected.)
2. Gaps: what is missing that {OWNER} will need? (a booking that has no matching flight/hotel/ride, an event not on the
   calendar, a form not filled, a renewal or document that must happen first.)
3. Deadlines: the real cutoff, and a follow-up reminder before it if {OWNER} hasn't acted.
4. Preparation you can do now without permission (research, drafts, checklists, options with prices or times).
5. Assumptions: never assume facts you don't have (that {OWNER} has kids, where a company is, that something was paid);
   say what you'd check first.
6. Personal moments (birthdays, anniversaries, family arrivals) matter as much as business: give a heads-up early
   enough to plan, and offer the useful next step.
7. Close the loop: offer to DO the obvious thing yourself (book the ride, draft the reply, pre-fill the known parts of
   the form, research options with prices, keep watching the flight) instead of telling {OWNER} to do it; give the full
   set of sensible options for a conflict (move or shorten either meeting, delegate, join remotely) with your pick.
8. Restraint: if open_opportunities shows it is already handled or scheduled, or nothing new needs doing, return an
   empty list. Don't invent gaps without evidence; don't add busywork.
9. Sending {OWNER} a finished brief, result or heads-up is notify_owner_now (happens automatically); remind_owner is
   only for offering a reminder at a future time.
10. Delivery: urgency now = text {OWNER} right away; today = text at surface_at; later = a text at a useful time, held
   outside quiet hours. Don't call something urgent unless delay costs {OWNER} something.
Practical defaults (use unless the item says otherwise): arrive 75-90 min before a domestic flight at small airports,
2 h at large hubs and 3 h international; add the drive time from where {OWNER} is to get a leave time. Most airlines
open check-in 24 h before departure. Few refills left means the doctor must renew soon. Renewals and trials usually
need action a few days before the cutoff. Gifts that ship need about a week.
11. Only {OWNER}'s world: an event on someone else's calendar matters only if {OWNER} is invited, affected or it
   collides with their own plans; otherwise set owner_involved no. Never write about {OWNER} in the third person in
   `text`: talk to them ("Your 8:30 overlaps...", "Want me to...?").
12. Already handled: if the observation says owner_already_replied is true, or later_in_thread shows the matter has
   moved on, {OWNER} is on it. Return nothing unless something concrete is still open that their reply did not cover.
   owner_preferences lists things {OWNER} told you to stop raising or handles personally: never raise those.
   recently_handled lists what you already dealt with or told {OWNER} about this week. Do not raise one of those again
   unless this observation changes it (cancelled, moved, a new deadline, a new ask from someone); then say what
   changed in the title.
13. Finish the thinking. The plan is read by someone who has not seen the source, so every step carries its own
   specifics. First decide the ONE main thing (the gap, clash or risk that hurts most if nobody acts: the missing
   flight, the meeting {OWNER} cannot reach, the deadline that will pass) and put it in the title, in `why` and in the
   first step. Everything else that is real (a cancellation window, a renewal choice, a smaller ask) stays in the plan,
   after it. This is about order, never a reason to drop an item: an expiring plan, warranty or deadline with a
   choice to make is still worth raising. (a) Work out now whatever can be worked out from what you are shown: a deadline counted from a date, a
   time-zone conversion, a run-out date, a leave time, a price difference. State the result with weekday and date.
   Never write "calculate", "determine" or "this weekend", and do not say "may" about something you can establish (a
   public holiday, simple arithmetic). (b) The first thing {OWNER} hears must stand on its own: the exact deadline, every
   amount at stake (each price or fee the source gives), who to call with the number or reference shown, and what
   happens if nothing is done. Never hold a number or a contact back for a later step. (c) Give every sensible option
   the source makes visible (an earlier or later alternative, a backup person, doing it the day before), with your
   pick. (d) Preparation is never something to ask permission for: list it as a free step and say what will be ready
   (the draft, the checklist, the order filled in). Not knowing something is a reason to look it up, not to leave it
   out: before a meeting or call with someone new, research who they and their company are (what they do, size,
   location), check {OWNER}'s past emails with them, and prepare two or three sharp questions. (e) Quiet hours limit
   what reaches {OWNER}, not silent work: a free self-service step (a check-in) happens the moment it opens, even at
   6am; only the message about it waits for {QUIET_END}.
14. Label honestly. A step that needs {OWNER}'s yes gets the class that needs it (spend_money, message_outside_person,
   calendar_change, delete_or_cancel). Changing a booking, an order or a reservation is never self_service_free, even
   when it costs nothing; self_service_free is only for things like a check-in that change nothing {OWNER} decided.
15. Whose move is it? For an email, `addressed` says how it reached {OWNER}: "to" (it is addressed to them), "cc" (they
   are only copied) or "not_named" (a list, an alias or a blind copy). When {OWNER} is only copied or not named, and the
   email is one person asking another person for something, the next move is theirs, not {OWNER}'s: {OWNER} was copied
   to stay informed. Return an empty list, however much money or urgency the thread mentions, unless (a) the text asks
   {OWNER} personally for something, by name or plainly meaning them; (b) the people it is addressed to are not
   handling it and the cost lands on {OWNER} (a deadline about to pass, money about to be lost); or (c) it changes one
   of {OWNER}'s own plans or commitments. If you do raise it, set owner_involved honestly ("no" when it is someone
   else's to handle) and say in `why` who has the next move. When it is addressed to {OWNER}, or `addressed` is
   missing, this rule changes nothing.
   And whoever it is addressed to: something whose best next step is to leave it alone is not an opportunity.
Put the whole plan for one real-world thing into ONE opportunity: list every step in `steps`, each with its own
action_class (adding or moving a calendar event is calendar_change; texting or emailing anyone but {OWNER} is
message_outside_person; any purchase or fee is spend_money). action_class of the opportunity = its main step; use separate
opportunities only for separate things. Surface early enough to be useful (same day if action may be needed soon).""")


BACKEND = os.environ.get('LIFE_BACKEND', 'claude')          # 'claude' (desktop shell) or 'hermes' (headless, Zeus's own route)
HERMES_MODEL = os.environ.get('LIFE_HERMES_MODEL') or settings.get('models.main', 'gpt-6.2-sol')
BACKUP_MODEL = os.environ.get('LIFE_HERMES_BACKUP_MODEL') or settings.get('models.backup', 'claude-sonnet-5-5')
HERMES_AGENT = str(settings.HERMES_AGENT_DIR)
ROUTE_TASK = settings.get('models.route_task', 'life_noticer')   # the profile's auxiliary task whose routes list these models

_stats_lock = threading.Lock()        # the note's checks and votes call complete() from a few threads at once
_stats = {'answered': {}, 'failed': {}, 'no_answer': 0}


def stats() -> dict:
    """Which model answered this process's calls so far: {'answered': {model: n}, 'failed': {model: n}, 'no_answer': n}.
    One tick is one process, so this is "this run". tick.py writes it into the health note."""
    with _stats_lock:
        return {'answered': dict(_stats['answered']), 'failed': dict(_stats['failed']), 'no_answer': _stats['no_answer']}


def _count(what: str, model: str = '') -> None:
    with _stats_lock:
        if model:
            _stats[what][model] = _stats[what].get(model, 0) + 1
        else:
            _stats[what] += 1


@contextmanager
def _profile_context():
    """Run Hermes's own model client inside this agent's profile (its config, keys and secret scope)."""
    if HERMES_AGENT not in sys.path:
        sys.path.insert(0, HERMES_AGENT)
    import hermes_bootstrap  # noqa: F401  (sets up Hermes's import paths)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import (build_profile_secret_scope, set_secret_scope, reset_secret_scope,
                                    set_multiplex_context, reset_multiplex_context)
    home = settings.PROFILE_DIR
    ht = set_hermes_home_override(home)
    try:
        mt = set_multiplex_context(True)
        try:
            st = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
            try:
                yield
            finally:
                reset_secret_scope(st)
        finally:
            reset_multiplex_context(mt)
    finally:
        reset_hermes_home_override(ht)


def _route_list(task):
    from hermes_cli.config import load_config
    entry = load_config()['auxiliary'][task]
    return [entry, *(entry.get('fallback_chain') or entry.get('fallback_providers') or [])]


def _closed_schema(schema):
    """A copy of `schema` where every object node says additionalProperties: false unless it already says something.
    Some providers' structured output refuses a schema without it. The caller's own schema is never changed."""
    if isinstance(schema, list):
        return [_closed_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _closed_schema(v) for k, v in schema.items()}
    t = out.get('type')
    if (t == 'object' or (isinstance(t, list) and 'object' in t) or 'properties' in out) and 'additionalProperties' not in out:
        out['additionalProperties'] = False
    return out


def _schema_instruction(schema):
    """The schema written into the instructions too: some routes do not pass response_format on to the model, so the
    field names must also be in the text the model reads."""
    if not isinstance(schema, dict):
        return '\nReply with one JSON object matching the schema.'
    return ('\nReply with one JSON object matching this JSON schema. Use exactly these field names:\n'
            + json.dumps(schema, ensure_ascii=False, separators=(',', ':')))


def _shaped(out, schema):
    """A reply that skips the schema's top-level required fields is not an answer: raise, so the backup model is tried
    instead of acting on a half reply."""
    if not isinstance(out, dict):
        raise ValueError('The background model did not return a JSON object')
    schema = schema if isinstance(schema, dict) else {}
    missing = [k for k in schema.get('required') or [] if k not in out]
    if missing:
        raise ValueError('The background model reply is missing required fields: ' + ', '.join(map(str, missing)))
    return out


def _hermes_call(model: str, system: str, prompt: str, schema: dict, timeout) -> dict:
    """Call one named route from this profile's background reader settings (auxiliary task ROUTE_TASK)."""
    with _profile_context():
        from agent.auxiliary_client import resolve_provider_client, _build_call_kwargs
        from hermes_constants import parse_reasoning_effort
        route = next(r for r in _route_list(ROUTE_TASK) if r['model'] == model)
        provider = route['provider']
        reasoning = parse_reasoning_effort(route['reasoning_effort'])
        if not provider or provider == 'auto' or not model or reasoning is None:
            raise ValueError('A concrete model, provider and effort are required')
        client, selected = resolve_provider_client(provider, model, explicit_base_url=route.get('base_url'),
                                                   api_mode=route.get('api_mode'))
        if client is None or selected != model:
            raise RuntimeError('The requested provider and model could not be loaded')
        kwargs = _build_call_kwargs(provider, model, [{'role': 'system', 'content': system + _schema_instruction(schema)},
                                                      {'role': 'user', 'content': prompt}], timeout=timeout,
                                    base_url=route.get('base_url'), reasoning_config=reasoning)
        if schema is not None:
            kwargs['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'out', 'schema': _closed_schema(schema),
                                                                                'strict': False}}
        result = client.chat.completions.create(**kwargs)
        if not result.choices or not result.choices[0].message.content:
            raise ValueError('Empty background model result')
        text = result.choices[0].message.content
        return _shaped(json.loads(text[text.find('{'):text.rfind('}') + 1]), schema)


def answer_with_backup(call) -> tuple | None:
    """The one place the backup rule lives. `call(model)` makes one model call. The main model goes first, every time. If that
    call raises or comes back with nothing readable, ONE try on the configured backup.
    -> (answer, the model that gave it), or None when both failed. Every outcome is counted (stats())."""
    order = [HERMES_MODEL] + ([BACKUP_MODEL] if BACKUP_MODEL and BACKUP_MODEL != HERMES_MODEL else [])
    for model in order:
        try:
            out = call(model)
        except Exception:
            out = None
        if out is not None:
            _count('answered', model)
            return out, model
        _count('failed', model)
    _count('no_answer')
    return None


def complete(system: str, prompt: str, schema: dict, timeout=150) -> dict | None:
    """-> {'out': dict, 'cost': float|None} or None. Same contract for both backends. On the hermes backend the
    answer also says which model gave it ('model'). Every model step of the tick comes through here (the noticer and
    its review, the note's fact readers and votes, the night votes, the learning steps), so they all share the backup."""
    if BACKEND == 'hermes':
        got = answer_with_backup(lambda model: _hermes_call(model, system, prompt, schema, timeout))
        return None if got is None else {'out': got[0], 'cost': None, 'model': got[1]}
    if not EMPTY_MCP.exists():
        EMPTY_MCP.write_text('{"mcpServers":{}}')
    cmd = [CLAUDE, '-p', '--setting-sources', '', '--strict-mcp-config', '--mcp-config', str(EMPTY_MCP),
           '--disable-slash-commands', '--no-session-persistence', '--tools', '', '--exclude-dynamic-system-prompt-sections',
           '--system-prompt', system, '--model', MODEL, '--output-format', 'json', '--json-schema', json.dumps(schema), prompt]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, 'HOME': str(settings.HOME)}, cwd=tempfile.gettempdir())
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    if d.get('is_error') or not isinstance(d.get('structured_output'), dict):
        return None
    return {'out': d['structured_output'], 'cost': d.get('total_cost_usd')}


# Added to the instructions ONLY when known_people is not empty (people.py). With nobody known the instructions
# are unchanged.
PEOPLE_RULE = _fill("""

People {OWNER} has told you about: known_people lists them, with who they are to {OWNER} and what {OWNER} said, in
{OWNER}'s own words. Read it before you decide. When a note says things about a person are not for {OWNER}, an item that
is only about that person or their work is not {OWNER}'s: return an empty list, however it is addressed. If the same
item also asks something of {OWNER} personally, raise only that part. Use `relation` instead of guessing who someone
is. A person who is not listed is simply not known yet, never a reason to drop an item.""")
PEOPLE_REVIEW = _fill("""
- known_people: when a note says things about a person are not for {OWNER}, an item that is only about that person
  stays out. Never add it back. If the item also asks something of {OWNER} personally, that part stays in.""")


# Added ONLY when owner_preferences carries what the learning steps wrote (standing rules, the nightly page). Without
# it, a "SECOND NOTICE: Action needed" email about something the owner already said to drop can be raised anyway.
LEARNED_RULE = _fill("""

What {OWNER} has told you in chat: owner_preferences may hold lines that start "- His standing rule:" (the marker
for one of {OWNER}'s standing rules) and a block about how {OWNER} wants you to work with them. These are {OWNER}'s own
words and they bind you. An item that is only about something listed under "Do not bring up again", or that a
standing rule says to leave alone, is not an opportunity: return an empty list, even when the sender calls it a final
notice, names a deadline or says action is needed. {OWNER} has already decided. Raise it only if something new has
happened that {OWNER} has not ruled on (a different amount, a new problem, someone new asking {OWNER} directly), and
then raise only the new part. A rule about one kind of thing never silences a different kind of thing.""")
LEARNED_REVIEW = _fill("""
- owner_preferences: a "Do not bring up again" line or a standing rule is {OWNER}'s decision. An item that is only
  about that stays out, whatever the sender's urgency. Never add it back. Something new {OWNER} has not ruled on stays in.""")


def learned(context: dict) -> bool:
    """True when the preferences text carries lines only the learning steps write. Never true with the switch off."""
    prefs = str(context.get('prefs') or '')
    return 'Do not bring up again:' in prefs or '- His standing rule:' in prefs


def ask(observation: dict, context: dict, timeout=150) -> dict | None:
    prompt = json.dumps({'now': context.get('now'), 'observation': observation,
                         'nearby_calendar': context.get('nearby_calendar') or [],
                         **({'calendar_coverage': context['coverage']} if context.get('coverage') else {}),
                         'open_opportunities': context.get('open') or [],
                         'known_people': context.get('people') or [],
                         'owner_preferences': context.get('prefs') or '',
                         'recently_handled': context.get('handled') or []}, default=str, ensure_ascii=False)[:28000]
    r = complete(SYSTEM + (PEOPLE_RULE if context.get('people') else '') + (LEARNED_RULE if learned(context) else ''), prompt, SCHEMA, timeout)
    if r is None or not isinstance(r['out'], dict):
        return None
    return {'opportunities': r['out'].get('opportunities') or [], 'cost': r['cost']}


def whose(observation: dict, opp: dict) -> str:
    """Is this one the owner's? -> 'yes' | 'no' | 'unknown' (policy.py: 'no' is never raised, 'unknown' never
    interrupts the owner). Mail addressed to the owner is theirs. Mail the owner is only copied on is whatever the
    noticer read it as (arriving in the owner's inbox alone does not make it theirs). Calendar events keep the
    noticer's reading."""
    read = opp.get('owner_involved') if opp.get('owner_involved') in ('yes', 'no', 'unknown') else None
    if observation.get('source') == 'calendar':
        return read or 'unknown'
    if observation.get('addressed', 'to') == 'to':
        return 'yes'
    return read or 'unknown'


def _flat(o):
    return re.sub(r'\s+', ' ', json.dumps(o, default=str, ensure_ascii=False)).lower()


def check(observation: dict, opps: list, context: dict | None = None) -> list:
    """Drop opportunities whose evidence isn't in what the model was shown (observation + nearby calendar + open
    items); quotes stitched with '...' or ';' are checked piece by piece (each piece >= 70% of its words present)."""
    hay = _flat({'o': observation, 'c': (context or {}).get('nearby_calendar') or [], 'p': (context or {}).get('open') or []})
    out = []
    for o in opps:
        ev = str(o.get('evidence') or '').lower()
        pieces = [x for x in re.split(r'\.\.\.|…|;|\n', ev) if re.search(r'\w{3,}', x)]
        def ok(piece):
            words = re.findall(r'[a-z0-9]{3,}', piece)
            return bool(words) and sum(w in hay for w in words) >= 0.7 * len(words)
        o['evidence_ok'] = bool(pieces) and all(ok(x) for x in pieces)
        if not o['evidence_ok'] or o.get('action_class') not in CLASSES or o.get('action_class') == 'none_needed':
            continue
        o['dedupe_key'] = re.sub(r'[^a-z0-9:._@-]+', '-', str(o.get('dedupe_key') or o.get('title')).lower())[:120]
        out.append(o)
    return out


REVIEW = _fill("""You are {OWNER}'s exacting executive assistant reviewing a junior assistant's draft plan for ONE observation
before anything reaches {OWNER}. Return the corrected list of opportunities (same schema). Rules:
- If nothing genuinely needs doing (newsletters, promos, receipts, FYIs, things already handled or scheduled in
  open_opportunities, cancellations already off the calendar, phishing), return an EMPTY list. Silence beats noise.
- Remove busywork and speculative gaps with no evidence. Keep one opportunity per real-world thing.
- Whose move is it? If `addressed` shows {OWNER} is only copied ("cc") or not named, and other people are handling it
  between themselves, return an EMPTY list unless the text asks {OWNER} directly, or the cost of nobody acting lands on
  {OWNER}.
- Add anything a great executive assistant would obviously cover that the draft missed: concrete leave/arrival times with
  time zones, the conflict with nearby events and the full set of options with a pick, the missing booking or
  document, the real deadline plus a follow-up before it, the prep {OWNER} will need, and offering to DO the next step.
- Never assume facts {OWNER} hasn't given (kids, locations, payments); check first.
- Each step's action_class must match what the step does (calendar edits = calendar_change, anything to another
  person = message_outside_person, any cost = spend_money). Timing: early enough to be useful, never routine before
  quiet hours end ({QUIET_END} on {OWNER}'s clock), urgent items now.
Keep evidence as exact quotes from the observation or context.""")


def review(observation: dict, context: dict, draft: list, timeout=150) -> dict | None:
    prompt = json.dumps({'now': context.get('now'), 'observation': observation,
                         'nearby_calendar': context.get('nearby_calendar') or [],
                         **({'calendar_coverage': context['coverage']} if context.get('coverage') else {}),
                         'open_opportunities': context.get('open') or [],
                         **({'known_people': context['people']} if context.get('people') else {}),
                         **({'owner_preferences': context.get('prefs') or ''} if context.get('people') or learned(context) else {}),
                         'draft': draft}, default=str, ensure_ascii=False)[:30000]
    people, said = bool(context.get('people')), learned(context)
    r = complete(SYSTEM + (PEOPLE_RULE if people else '') + (LEARNED_RULE if said else '') + "\n\n" + REVIEW
                 + (PEOPLE_REVIEW if people else '') + (LEARNED_REVIEW if said else ''), prompt, SCHEMA, timeout)
    if r is None or not isinstance(r['out'], dict):
        return None
    return {'opportunities': r['out'].get('opportunities') or [], 'cost': r['cost']}


def notice(observation: dict, context: dict) -> dict | None:
    """Draft, then self-review. Falls back to the draft if the review call fails."""
    first = ask(observation, context)
    if first is None:
        return None
    second = review(observation, context, first['opportunities'])
    if second is None:
        return first
    return {'opportunities': second['opportunities'], 'cost': (first.get('cost') or 0) + (second.get('cost') or 0),
            'draft_count': len(first['opportunities'])}
