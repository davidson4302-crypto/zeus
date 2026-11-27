"""Policy: the owner's autonomy rules. The model proposes; this decides.

auto  - do it now, tell the owner after (prep, research, reminders/notes to the owner, free reversible self-service like check-in)
offer - propose with a one-word yes (useful but optional: e.g. "want a reminder to call her?")
ask   - needs the owner's yes before anything happens (money, outside people, calendar changes/RSVPs, deletes)
skip  - not worth the owner's attention
A grant ("automate this", stored by family) turns ask/offer into auto for that family; the agent then owns it fully.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import settings
from ownertime import PACIFIC, alert_open  # the zone the owner is in right now: home time unless they said otherwise (ownertime.py)

# How the model's step text names the owner: their first name, or "the owner". Used inside the patterns below.
_ME = '(?:' + '|'.join(dict.fromkeys(re.escape(n.lower()) for n in (settings.OWNER_FIRST, 'the owner') if n)) + ')'
BASE = {
    'prepare_or_research': 'auto',
    'remind_owner': 'auto',   # reminders to the owner are free work
    'notify_owner_now': 'auto',
    'self_service_free': 'auto',
    'spend_money': 'ask',
    'message_outside_person': 'ask',
    'calendar_change': 'ask',
    'delete_or_cancel': 'ask',
}


# Where a word can be a command and not a noun: at the start of a sentence or clause, or after and/then/to/will...
_DO = r"(?:^|[.;,:(]\s*|\b(?:and|or|then|to|will|would|should|can|could|please|also|just|simply|automatically|now|I|we)\s+)"
# ...followed by something that looks like its object ("book it", "pay the fee", "order 24"), not "purchase volumes".
# (The capital-letter test must stay case-sensitive even inside a case-insensitive pattern.)
_OBJ = r"(?:\s+(?:only|just|also|then|immediately|directly|now|promptly|online|or\s+\w+))?\s+(?:(?:it|them|this|that|these|those|the|a|an|his|her|him|your|for|to|one|two|both)\b|[0-9$]|(?-i:[A-Z]))"
_OBJ_MSG = r"\s+(?:(?:it|them|this|that|these|those|the|a|an|his|her|him)\b|(?-i:[A-Z]))"
_ROLE = r"(?i:broker|organi[sz]er|coach|teacher|vendor|supplier|customer|client|landlord|contractor|attendees?|host|school|office|pharmacy|airline|hotel|kennel|agent|rep|company|provider|monitoring|support|GC)"
_NOT_A_PERSON = r"(?!(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b|(?:January|February|March|April|May|June|July|August|September|October|November|December)\b|(?i:" + _ME + r"|you|him|your|his)\b)"
_OWN = r"(?i:calendar|email|inbox|records?|files?|notes?|history|thread|messages?|correspondence|portal|account)"
PERMISSION_WORDS = [
    (re.compile(r"\b(add|put|create|block|hold|move|reschedul\w*|shorten|shift|decline|accept|rsvp|cancel)\b.{0,40}\b(calendar|event|meeting|invite|block|slot|hold|review|lunch|call)\b", re.I), 'calendar_change'),
    (re.compile(_DO + r"(pay|buy|purchase|order|book|reserve|upgrade|renew|subscribe|tip)\b(?!.{0,20}\b(options?|prices?|research)\b)(?:" + _OBJ + r"|\s*$|\s*[.;,])", re.I | re.M), 'spend_money'),
    (re.compile(r"\b(?i:email|text|message|reply to|respond to|write to|call|phone|send|ask|remind|tell|notify|inform|instruct|contact|ping|nudge|chase|update|alert|follow up with|reach out to|check with|coordinate with|confirm with|loop in|get back to)\b\s+(?!(?i:" + _ME + r"|him|you|your|his|the brief|a reminder)\b)(?:(?i:the|a|an)\s+)?(?:" + _NOT_A_PERSON + r"[A-Z][a-z]+|[A-Z]{2,}\b|" + _ROLE + r"\b)"), 'message_outside_person'),
    (re.compile(r"\b(delete|unsubscribe|cancel (?:the |my |his )?(?:trial|subscription|order|reservation|booking|plan))\b", re.I), 'delete_or_cancel'),
    # "... add the pickup and travel blocks to your calendar" (the calendar can be far from the verb) and answering
    # or editing an invitation.
    (re.compile(r"\b(add|put|create|block|hold|move|update|edit|correct|amend)\b[^.;]{0,120}?\b(?:to|on|in)\s+(?:the|his|your|my)\s+(?:\w+\s+)?calendars?\b"
                r"|\b(update|edit|correct|amend|accept|decline|answer|respond(?:ing)?\s+to|reply(?:ing)?\s+to)\b[^.;]{0,80}?\b(invit\w+|rsvp|calendar|events?|entr(?:y|ies)|meetings?)\b"
                r"|\b(add|create)\b[^.;]{0,80}?\b(blocks?|events?|entr(?:y|ies))\b", re.I), 'calendar_change'),
    # A message whose recipient is an address or a number, or that is sent "to" a named person or role.
    (re.compile(r"(?:^|[.;,:(]\s*|\b(?i:and|or|then|to|will|would|should|can|could|please|also|just|simply|automatically|now|I|we)\s+)(?i:email|text|message|call|phone|contact|write to|follow up with|reach out to|reply to|respond to|notify|send|forward)\b[^.;]{0,60}?(?:[\w.+-]+@[\w-]+\.[\w.-]+)"
                r"|\b(?i:send|email|forward|submit|text|deliver)\b[^.;]{0,60}?\b(?i:to)\s+(?:(?i:the|a|an)\s+)?(?:" + _NOT_A_PERSON + r"[A-Z][a-z]+|[A-Z]{2,}\b|" + _ROLE + r"\b)"
                r"|\b(?i:send|email|forward|submit)\s+(?:(?i:the|a|an|that|this)\s+)?(?:\w+\s+)?(?i:reply|email|message|note|request|form|letter|invoice|quote|notice|application)s?\b(?!\s+(?i:to\s+)?(?i:you|him|" + _ME + r")\b)"), 'message_outside_person'),
    # Changing a booking is not "free self-service like a check-in", even when the change costs nothing.
    (re.compile(_DO + r"(rebook|switch|change|move|reschedul\w*|swap|modify|extend|shorten)\b.{0,40}\b(flight|outbound|return leg|itinerary|reservation|booking|appointment|delivery|pick-?up|ticket|seat|room|stay|rental|boarding)\b", re.I | re.M), 'calendar_change'),
    # Check-ins run on their own; refills wait for the owner's yes.
    (re.compile(_DO + r"(request|submit|order|place|reorder|renew)\b.{0,40}\brefills?\b|" + _DO + r"refill\b.{0,20}\b(the|his|your)\s+(prescription|medication|rx)\b", re.I | re.M), 'spend_money'),
    # Putting something in front of an outside party in the owner's name (a form, a signature) is a message to them.
    (re.compile(r"(?:^|[.;,:(]\s*|\b(?:and|then|will|would|should|please|also|just|simply|automatically|now)\s+)(submit|upload|enroll|register|sign up|apply for|sign(?!\s+(?:in|up|off)\b))\b", re.I | re.M), 'message_outside_person'),
    # "Verify the cutoff with Jordan" / "confirm the dial-in with the organizer": finding something out BY asking a person.
    (re.compile(r"\b(?i:verify|confirm|check|clarify|coordinate|arrange|agree|discuss|negotiate|settle)\b(?:(?!\b" + _OWN + r"\b)[^.;]){0,60}?\b(?i:with)\s+(?:(?i:the|a|an)\s+)?(?:" + _NOT_A_PERSON
                + r"(?!(?:Dr|Mr|Mrs|Ms)\b)[A-Z][a-z]+(?=\s*(?:$|[,.;:)]|\s(?i:at|by|before|that|whether|to|about|on|for|if|and|so)\b))|" + _ROLE + r"\b)"), 'message_outside_person'),
]
INQUIRE = PERMISSION_WORDS[-1][0]
STRICT = {'spend_money', 'message_outside_person', 'calendar_change', 'delete_or_cancel'}
REMINDER = re.compile(r"\s*(remind\s+(him|you|" + _ME + r"|me)\b|(set|schedule|add|create|keep|send)\s+(him\s+|you\s+|" + _ME + r"\s+)?(a\s+|an\s+)?(reminder|nudge)\b)", re.I)
# "Check in, do not buy Priority Boarding" is not a purchase. Words that say what will NOT be done (and the few
# words after them, up to the next "and", "but", "then" or punctuation) are dropped before the permission words are
# looked for. When in doubt it still asks: "skip the upgrade and book the ride" keeps "book the ride".
NEGATED = re.compile(r"\b(?:do(?:es)? not|don'?t|did not|never|without|not|no|skip(?:ping)?|declin(?:e|ing)|avoid(?:ing)?|rather than|instead of)\b(?:[ \t]+(?!(?:and|but|then|while|so|before|after)\b)[^\s.;,:]+){1,6}", re.I)
# What a step only PROPOSES to the owner ("offer to contact Taylor", "ask approval to submit", "recommend moving it") is
# talk, not action: it is dropped the same way, to the end of its sentence.
# A list of alternatives the owner could choose from ("Keep alternatives ready: move X; shorten Y; ask Alex to cover") is
# a menu, not a set of actions. It is dropped up to the end of its sentence, across the semicolons.
ALTS = re.compile(r"\b(?:(?:keep|present|list|offer|give|include|prepare)\s+)?(?:(?:the|these|other|two|three|a few|his|your)\s+)?(?:alternatives?|options?|backups?|fallbacks?|choices)\b(?:\s+ready)?\s*:[^.]*", re.I)
PROPOSED = re.compile(r"\b(?:offer\w*\s+(?:to|again to)|present\w*\s+(?:(?:the|these|other)\s+)?(?:alternatives?|choices)|ask\w*\s+(?:for\s+)?(?:your\s+|his\s+)?(?:permission|approval|authori[sz]ation)(?:\s+to)?|(?:obtain|get|request|seek)\w*\s+(?:your\s+|his\s+)?(?:explicit\s+)?(?:permission|approval|authori[sz]ation)(?:\s+to)?"
                      r"|ask\w*\s+(?:you\s+|him\s+|" + _ME + r"\s+)?(?:whether|if|how|which|who|when|where|what)|recommend\w*|propos\w+|suggest\w*|present\w*\s+(?:the\s+)?options?)\b"
                      r"[^.;]*?(?=,?\s*\b(?:and|then)\s+(?:then\s+)?(?:pay|buy|book|order|purchase|send|email|text|submit|contact|call|phone|cancel|move|reschedule|accept|decline|sign|enroll|register|place|switch|change|rebook|update|add|follow up|reach out|notify|instruct)\b|[.;]|$)", re.I)
# A step whose own words say it waits for the owner's yes is never done on its own, whatever class the model gave it.
NEEDS_YES = re.compile(
    r"\b(?:with|after|once|upon|pending|subject to|given)\s+(?:(?:your|his|" + _ME + r"'?s)\s+)?(?:explicit\s+|prior\s+)?(?:approval|permission|authori[sz]ation|consent|agreement|go-ahead|sign-?off|okay|ok|yes)\b"
    r"|\bif\s+(?:you|he|" + _ME + r")\s+(?:approves?|agrees?|says?\s+yes|authori[sz]es?|confirms?|consents?|chooses?|picks?|decides?|opts?)\b"
    r"|\b(?:once|after|when)\s+(?:you|he|" + _ME + r")\s+(?:approves?|agrees?|says?\s+yes|authori[sz]es?|confirms?|picks?|chooses?|decides?)\b"
    r"|\bonly\s+(?:if|when|after|with)\s+(?:you|he|" + _ME + r"|your|his)\b", re.I)
TO_OWNER = {'notify_owner_now', 'remind_owner'}
# A sentence that starts with free work (looking, drafting) or with telling the owner is free up to the point where it
# goes on to act: "Verify the binder ... and then email Jordan" is not all preparation.
LEAD_IN = re.compile(r"^\s*(?:(?:only\s+if|only\s+when|in\s+case|if|once|when|after|before|unless|until|at|by|on|for|otherwise|then|also|first|next|finally|meanwhile)\b[^,:]*[,:]\s*)+", re.I)
PREP = re.compile(r"\s*(draft|prepare|research|look up|look for|look into|find|check(?!\s+(?:with|in\b))|compare|list|watch|track|search|review|verify|summari[sz]e|locate|read|identify|calculate|work out|monitor|gather|collect"
                  r"|assess|estimate|evaluate|analy[sz]e|map|outline|plan|make|write|assemble|compile|pre-?fill|note|use)\b", re.I)
TO_HIM = re.compile(r"\s*(tell|alert|text|notify|message|send|give|deliver|flag|explain|present|show|ask|remind|update|brief|warn|offer|recommend|report|surface|share)\b\s+(?:to\s+)?(?:you|him|" + _ME + r")\b"
                    r"|\s*(flag|explain|warn|recommend|report|surface)\b", re.I)
TO_HIM_LOOSE = re.compile(r"\s*(tell|alert|text|notify|message|send|give|deliver|flag|explain|present|show|ask|remind|brief|warn|offer|recommend|report|surface|share|include)\b(?:\s+(?:the|a|an|your|his|that|any|what|which|whether|today|now|again)\b|\s*:|\s*$)", re.I)
THEN_ACT = re.compile(r"(?:\band\b|\bthen\b|,|:)\s*(?:then\s+|also\s+)?(?:((?:send|email|text|submit|forward)\b(?!\s+(?:(?:it|them|this|that|the\s+\w+|a\s+\w+)\s+)?(?:to\s+)?(?:you|him|" + _ME + r")\b)(?=" + _OBJ_MSG + r")|(?:reply|respond)\b(?=\s+(?:to|with)\s+(?!(?:you|him|" + _ME + r")\b)))"
                      r"|(call|phone|contact|book|pay|buy|order|purchase|cancel|move|reschedule|accept|decline|rsvp|follow up with|reach out to|check with|coordinate with|confirm with|instruct|notify|inform)\b(?=" + _OBJ + r"))", re.I)


def _core(part: str, cls) -> str:
    """The sentence without its opening clause. The opener ("If none is recorded,", "In your Wednesday, November 11
    morning brief,") ends at the first comma or colon after which a free verb starts, as long as nothing before
    that point is itself an action."""
    stripped = LEAD_IN.sub('', part)
    if stripped != part:
        return stripped
    for m in re.finditer(r"[,:]\s+", part[:160]):
        rest = part[m.end():]
        if PREP.match(rest) or TO_HIM.match(rest) or (cls in TO_OWNER and TO_HIM_LOOSE.match(rest)):
            head = PROPOSED.sub(' ', NEGATED.sub(' ', part[:m.start()]))
            if not any(rx.search(head) for rx, _ in PERMISSION_WORDS):
                return rest
            break
    return part


def guard_class(step: dict) -> str:
    """Code, not the model, has the last word: a step whose words add/move calendar items, spend, message someone
    else or cancel/delete needs permission whatever class the model gave it. Drafting, researching and telling
    the owner do not. The step is read sentence by sentence, so preparation at the start cannot hide an action later;
    what it says it will NOT do, and what it only proposes to the owner, do not count as actions."""
    cls = step.get('action_class')
    text = str(step.get('do') or '')
    if cls in STRICT:
        return cls
    # A reminder TO THE OWNER ("remind them Saturday to call Taylor") is not Zeus calling Taylor. It stays a free
    # reminder unless it writes to a calendar.
    if cls == 'remind_owner' and REMINDER.match(text) and not re.search(r'\bcalendar\b', text, re.I):
        return cls
    for part in re.split(r"(?<=[.;!?])\s+|;\s*", ALTS.sub(' ', text)):
        if not part.strip():
            continue
        core = _core(part, cls)                            # "If none is recorded, prepare a reply to Jordan" starts with "prepare"
        said = PROPOSED.sub(' ', NEGATED.sub(' ', core))
        to_him = TO_HIM.match(core) or (TO_HIM_LOOSE.match(core) if cls in TO_OWNER else None)
        if not to_him and INQUIRE.search(said):
            return 'message_outside_person'               # "Verify the cutoff with Jordan" starts with a free verb but asks a person
        lead = PREP.match(core) or to_him
        if lead:
            m = THEN_ACT.search(said, lead.end())          # only what follows the free verb can turn it into an action
            if not m:
                continue                                  # all preparation, or all addressed to the owner
            if m.group(1) and cls not in TO_OWNER:
                return 'message_outside_person'           # "... and send it" / "then submit the form"
            said = said[m.start():]                       # for a step addressed to the owner, "then send the details" goes to them
        for rx, strict in PERMISSION_WORDS:
            if rx.search(said):
                return strict
    return cls


def family(dedupe_key: str) -> str:
    """'rx:cvs:3381' -> 'rx:cvs:3381'; 'birthday:sister:YYYY-MM-DD' -> 'birthday:sister'. Dates/years drop off."""
    parts = [p for p in str(dedupe_key).split(':') if not re.fullmatch(r'\d{4}(-\d{2}){0,2}', p)]
    return ':'.join(parts[:3])


class Grants:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def granted(self, key):
        g = self.load()
        fam = family(key)
        return any(fam == f or fam.startswith(f + ':') for f in g)

    def entry(self, key):
        """The standing OK that covers this key (what the owner approved, in their words), or None."""
        g = self.load()
        fam = family(key)
        return next((v for f, v in g.items() if (fam == f or fam.startswith(f + ':')) and isinstance(v, dict)), None)

    def grant(self, key, note):
        g = self.load()
        g[family(key)] = {'at': datetime.now(PACIFIC).isoformat(), 'note': note}
        self.path.write_text(json.dumps(g, indent=2))


def decide(opp: dict, grants: Grants | None = None, now: datetime | None = None, muted=None) -> dict:
    """Per step: free steps run, permission steps wait for the owner's yes. The opportunity's autonomy is the strictest
    step's (ask > offer > auto), unless a grant covers this family. That overall label never turns the whole item into a
    question; the worker does every free step first and only the permission step is put to the owner.
    muted: families the owner told Zeus to stop raising (prefs.py); those are skipped."""
    now = now or datetime.now(PACIFIC)
    cls = opp.get('action_class')
    fam = family(str(opp.get('dedupe_key', '')).split('#')[0])
    if muted and any(fam == f or fam.startswith(f + ':') for f in muted):
        return {'autonomy': 'skip', 'surface_at': now.isoformat(), 'channel': 'morning_brief', 'reason': 'the owner said to stop raising this',
                'steps': [], 'granted': False}
    granted = bool(grants and grants.granted(opp.get('dedupe_key', '')))
    requested = max(now, _moment(opp.get('surface_at')) or now)
    when = alert_open(requested)
    if opp.get('urgency') != 'now':
        for deadline in lands_at(opp):
            if when >= deadline - timedelta(minutes=30):
                # Keep the original hour of lead. Owner contact still obeys the quiet window.
                requested = max(now, min(requested, deadline - timedelta(hours=1)))
                when = alert_open(requested)
                break
    night_hold = when > requested
    steps = []
    for st in opp.get('steps') or [{'do': opp.get('proposed_action'), 'action_class': cls}]:
        st = {**st, 'action_class': guard_class(st)}
        a = BASE.get(st.get('action_class'), 'skip')
        if a == 'auto' and st.get('action_class') not in TO_OWNER and NEEDS_YES.search(str(st.get('do') or '')):
            a = 'ask'                      # "with your approval, switch the flight" labelled free self-service still waits
        by_grant = granted and a in ('offer', 'ask')
        if by_grant:   # a standing OK that came from an offer (it carries `only`) covers only the kind of step and the people that were offered, never money, a booking, the calendar or a delete; every other standing OK is untouched and standok.py is not even loaded for it
            try:
                _narrow = isinstance((grants.entry(opp.get('dedupe_key', '')) or {}).get('only'), list)
            except Exception:
                _narrow = False   # a plain standing OK is never narrowed by trouble
            if _narrow:
                try:
                    by_grant = bool(__import__('standok').step_covered(grants, opp, st))
                except Exception:
                    by_grant = False   # trouble checking a narrow standing OK means the owner is asked
        if by_grant:
            a = 'auto'
        st = {**st, 'autonomy': a, **({'granted': True} if by_grant else {})}
        if st.get('when'):
            st['when'] = step_time(st, opp)
        steps.append(st)
    rank = {'skip': 0, 'auto': 1, 'offer': 2, 'ask': 3}
    live = [x['autonomy'] for x in steps if x['autonomy'] != 'skip']
    autonomy = max(live, key=rank.get) if live else BASE.get(cls, 'skip')
    if (opp.get('confidence') or 0) < 0.5 or opp.get('owner_involved') == 'no':
        autonomy = 'skip'
    channel = 'text_now' if opp.get('urgency') == 'now' else 'text'   # no 'morning_brief' channel: a routine item has its own time, held outside quiet hours by alert_open
    if channel == 'text_now' and night_hold:
        channel = 'text'                # marked urgent, but nothing about it lands before quiet hours end: first thing in the morning
    out = {'autonomy': autonomy, 'surface_at': when.isoformat(), 'channel': channel, 'reason': f'{cls} -> {autonomy}',
           'steps': steps, 'granted': granted}
    if granted:                         # what the owner approved, so the job can tell "the same kind of thing" from something new
        e = grants.entry(opp.get('dedupe_key', '')) or {}
        out['granted_by'] = {'approved_example': e.get('title'), 'his_words': e.get('his_words') or e.get('note')}
    if night_hold:
        out['held'] = HELD_FOR_MORNING
    return out


def _moment(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(PACIFIC) if value else None
    except ValueError:
        return None


def lands_at(opp) -> list:
    """The times the item itself gives for when something happens: its deadline, the start of its event."""
    return [d for d in (_moment(opp.get('deadline')), _moment(opp.get('event_start'))) if d]


HELD_FOR_MORNING = "Owner contact waits until quiet hours end on the owner's clock. Silent preparation may continue."


def step_time(step: dict, opp: dict, can_wait=None):
    when = step.get('when')
    if step.get('autonomy') in ('ask', 'offer') or step.get('action_class') in TO_OWNER:
        at = _moment(when)
        if at is not None:
            opening = alert_open(at)
            if opening > at:
                return opening.isoformat()
    return when
