"""enddate: turn the owner's own words for "how long" into a calendar day, by code.

"this week", "until Friday", "for two weeks", "through November 20". A model is good at spotting such words and bad at
counting days, so the model only copies the owner's words and this file does the counting. Two callers: the memory ladder
(ladder.py: a passing fact stops being true after this day) and `life zone` (lifezone.py: the owner is back home after it).

Rules, all of them on purpose:
- The day returned is the LAST day the thing still holds. "until Friday" holds through Friday.
- A weekday means its next occurrence, today included ("until Friday" said on a Friday is today).
- A bare day of the month ("till the 20th", "through the 1st") means the next such day, today included, rolling into
  the next month (and year) when this month's has gone by or does not exist. Only when the caller asks
  (month_day=True): the memory ladder does, behind its switch. `life zone` does not, so its answers stay as they are;
  passing month_day=True there is a one-line change.
- "this week" runs through Sunday. Too long by a day or two is the mild mistake; too short drops a true thing.
- Words this file does not know give None. Unknown is never turned into a guess ("for a few days", "for a while").
- The day is never before the day the owner spoke and never more than a year after it.
- "N days after <a day>" counts from that day, not from the day the owner spoke ("getting it done thursday, no driving
  for 10 days after" holds 10 days past Thursday). Only when the caller hands over the owner's whole message
  (context=...): the memory ladder does, behind its switch; `life zone` does not. The day is looked for in the owner's
  words first, then later in the same sentence, then the nearest day the owner named before them. No day found: None,
  never a count from the day the owner spoke.
- The model's copy of the owner's words may drop what the count starts from ("for a week" out of "off for a week after
  the wedding on saturday"). With the owner's message given, the owner's own sentence around the copied words is read
  and an anchored phrase there wins. Same callers, same switch as above.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december')
NUMBERS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
           'a couple of': 2, 'couple of': 2, 'a couple': 2}
MAX_DAYS = 366
_UNTIL = r"(?:until|till|til|'til|through|thru|by|back|home)"
_DAY = r'(?:' + '|'.join(d + '|' + d[:3] for d in WEEKDAYS) + r'|tues|thur|thurs)'
_MONTH = r'(?:' + '|'.join(m + '|' + m[:3] for m in MONTHS) + r'|sept)'


def _flat(words) -> str:
    s = str(words or '').replace('’', "'").replace('‘', "'").lower()
    return re.sub(r'\s+', ' ', re.sub(r"[^a-z0-9/' -]+", ' ', s)).strip()


def _weekday(name: str) -> int:
    return next(i for i, d in enumerate(WEEKDAYS) if d.startswith(name[:3]))


def _next_weekday(said: date, name: str) -> date:
    return said + timedelta(days=(_weekday(name) - said.weekday()) % 7)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _month_day(said: date, month: int, day: int):
    """That month and day, this year, or next year when it has already gone by."""
    for year in (said.year, said.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= said:
            return d
    return None


def _next_month_day(said: date, day: int):
    """The next date on or after `said` that falls on this day of the month ("the 31st" said Nov 15 is Dec 31)."""
    if not 1 <= day <= 31:
        return None
    y, mo = said.year, said.month
    for _ in range(13):
        if day <= calendar.monthrange(y, mo)[1] and date(y, mo, day) >= said:
            return date(y, mo, day)
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return None


# A day the owner names in a sentence. Only whole weekday names here ("sat" and "wed" are everyday words).
_NAMED_DAY = r'(?:' + '|'.join(WEEKDAYS) + r'|tues|thur|thurs|weds)'
_AFTER = re.compile(r'\b(\d{1,3}|a couple of|couple of|' + '|'.join(k for k in NUMBERS if ' ' not in k) +
                    r')\s+(?:more\s+)?(day|night|week|month)s?\s+(?:after|following|from)\b(.*)$')


def _plus(start: date, n: int, unit: str) -> date:
    if unit == 'month':
        mo = start.month - 1 + n
        y, mo = start.year + mo // 12, mo % 12 + 1
        return date(y, mo, min(start.day, calendar.monthrange(y, mo)[1]))
    return start + timedelta(days=n * (7 if unit == 'week' else 1))


def named_days(text, said: date) -> list:
    """Every day named in some of the owner's words, in order: [(position, date)]. A weekday is its next
    occurrence, today included ("next" pushes a day of this week on a week, "last" looks back); today, tonight, tomorrow;
    a month and day; "the 18th"; 3/18."""
    w = _flat(text)
    out, taken = [], []
    for m in re.finditer(r'\b(' + _MONTH + r')\s+(\d{1,2})(?:st|nd|rd|th)?\b|\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(' + _MONTH + r')\b', w):
        name, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        d = _month_day(said, next(i for i, mo in enumerate(MONTHS) if mo.startswith(name[:3])) + 1, int(day))
        if d is not None:
            out.append((m.start(), d))
            taken.append(m.span())
    for m in re.finditer(r'\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b', w):
        if not any(a <= m.start(1) < b for a, b in taken):
            d = _next_month_day(said, int(m.group(1)))
            if d is not None:
                out.append((m.start(), d))
    for m in re.finditer(r'\b(\d{1,2})/(\d{1,2})\b', w):
        d = _month_day(said, int(m.group(1)), int(m.group(2)))
        if d is not None:
            out.append((m.start(), d))
    for m in re.finditer(r'\b(?:(next|this|last)\s+)?(' + _NAMED_DAY + r')\b', w):
        d = _next_weekday(said, m.group(2))
        if m.group(1) == 'next' and d - said < timedelta(days=7) and _weekday(m.group(2)) >= said.weekday():
            d += timedelta(days=7)
        elif m.group(1) == 'last':
            d -= timedelta(days=7)
        out.append((m.start(), d))
    for m in re.finditer(r'\b(today|tonight|tomorrow)\b', w):
        out.append((m.start(), said + timedelta(days=1 if m.group(1) == 'tomorrow' else 0)))
    return sorted(out, key=lambda x: x[0])


def _after_day(w: str, said: date, context):
    """"for 10 days after", "two weeks after thursday", "3 days from the 18th" -> the last day, counted
    from the day the owner named. None when the words are not of this shape (the other rules then read them), False when they
    are but no day can be found (unknown: never counted from the day the owner spoke)."""
    m = _AFTER.search(w)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
    unit, rest = m.group(2), m.group(3)
    if re.match(r'\s*(?:now|today)\b', rest):
        return _plus(said, n, unit)
    days = named_days(rest, said)
    start = days[0][1] if days else None
    if start is None:
        sentences = [_flat(s) for s in re.split(r'(?<=[.!?\n])', str(context or '').replace('’', "'"))]
        for k, s in enumerate(sentences):
            i = s.find(w)
            if i < 0:
                continue
            later = named_days(s[i + len(w):], said)
            earlier = named_days(' '.join(sentences[:k] + [s[:i]]), said)
            start = later[0][1] if later else earlier[-1][1] if earlier else None
            break
    return False if start is None else _plus(start, n, unit)


_FOR = re.compile(r'\bfor\s+(?:the\s+)?(?:next\s+|another\s+)?(\d{1,3}|a couple of|couple of|' + '|'.join(k for k in NUMBERS if ' ' not in k) +
                  r')\s+(?:more\s+)?(day|night|week|month)s?\b')


def _in_owner_words(w: str, said: date, context):
    """The model's copy of the owner's words can leave out what the count
    starts from: "for a week" out of "im off for a week after the wedding on saturday". The owner's own sentence around the
    copied words is read, and an anchored phrase there wins:
      - "N days/weeks after <a day>" that starts inside the copied words ("for a week" + "after ... saturday");
      - "after <a day> ... for N days/weeks" with the copied words being the "for N" part.
    -> the last day (a date), False when the sentence anchors the count but names no day (unknown, never a count from
    the day the owner spoke), or None when the owner's sentence has no anchor (the other rules then read the words as before)."""
    for s in re.split(r'(?<=[.!?\n])', str(context or '').replace('’', "'")):
        s = _flat(s)
        i = s.find(w)
        if i < 0:
            continue
        tail = s[i:]
        m = _AFTER.search(tail)
        if m and m.start() < len(w):
            return _after_day(tail[m.start():], said, s)
        m = _FOR.search(w)
        before = re.search(r'\bafter\b(.{0,60})$', s[:i])
        if m and before:
            days = named_days(before.group(1), said)
            if days:
                return _plus(days[-1][1], int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)], m.group(2))
        return None
    return None


def end_day(words, said: date, month_day: bool = False, context=None):
    """-> the last day it holds (a date), or None when the words give no end this file can count.
    month_day=True also reads a bare day of the month ("till the 20th"). context: the owner's whole message; given, "N days
    after <a day>" counts from that day."""
    w = _flat(words)
    if not w:
        return None
    sunday = said + timedelta(days=6 - said.weekday())
    found = None
    if context is not None:
        found = _after_day(w, said, context)
        if found is None:
            found = _in_owner_words(w, said, context)      # an anchor in the owner's own sentence wins
        if found is False:
            return None
    m = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', w)
    if m:
        try:
            found = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if found is None:
        m = re.search(r'\b(?:before|ahead of)\s+(?:next\s+|this\s+)?(' + _DAY + r')\b', w)
        if m:                                        # "before Friday" stops the day before
            found = _next_weekday(said, m.group(1)) - timedelta(days=1)
            if found < said:
                found += timedelta(days=7)
    if found is None:
        m = re.search(r'\b' + _UNTIL + r'\s+(?:the\s+)?(?:end of\s+)?(?:(next|this|the)\s+)?(week|weekend|month|year)\b', w)
        if m:                                        # "until the end of the month", "through next week"
            ahead, span = m.group(1) == 'next', m.group(2)
            if span in ('week', 'weekend'):
                found = sunday + timedelta(days=7 if ahead else 0)
            elif span == 'month':
                y, mo = (said.year + (said.month == 12), said.month % 12 + 1) if ahead else (said.year, said.month)
                found = _month_end(y, mo)
            else:
                found = date(said.year + (1 if ahead else 0), 12, 31)
    if found is None:
        m = re.search(r'\b' + _UNTIL + r'\s+(?:the\s+)?(' + _MONTH + r')\s+(\d{1,2})(?:st|nd|rd|th)?\b', w) or \
            re.search(r'\b' + _UNTIL + r'\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(' + _MONTH + r')\b', w)
        if m:
            a, b = m.group(1), m.group(2)
            name, day = (a, int(b)) if not a.isdigit() else (b, int(a))
            found = _month_day(said, next(i for i, mo in enumerate(MONTHS) if mo.startswith(name[:3])) + 1, day)
            if found is None:
                return None
    if found is None and month_day:
        # "till the 20th", "through the 1st", "until 3rd": a day of the month with no month. Needs "the" or the
        # ordinal ending, so "until 3" (a clock time) never reads as a day.
        m = re.search(r'\b' + _UNTIL + r'\s+(?:the\s+(\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th))\b(?!\s*(?:/|:|am\b|pm\b|o\'?clock|of\b|week|month|quarter|half|time|round|grade|floor))', w)
        if m:
            found = _next_month_day(said, int(m.group(1) or m.group(2)))
            if found is None:
                return None
    if found is None:
        m = re.search(r'\b' + _UNTIL + r'\s+(\d{1,2})/(\d{1,2})\b', w)
        if m:
            found = _month_day(said, int(m.group(1)), int(m.group(2)))
            if found is None:
                return None
    if found is None:
        m = re.search(r'\b' + _UNTIL + r'\s+(?:(next)\s+|this\s+|on\s+)?(' + _DAY + r')\b', w)
        if m:
            found = _next_weekday(said, m.group(2))
            if m.group(1) and found - said < timedelta(days=7) and _weekday(m.group(2)) >= said.weekday():
                found += timedelta(days=7)           # "until next Friday" said on a Monday is the Friday after this one
    if found is None:
        m = re.search(r'\b' + _UNTIL + r'\s+(today|tonight|tomorrow)\b', w)
        if m:
            found = said + timedelta(days=1 if m.group(1) == 'tomorrow' else 0)
    if found is None:
        m = re.search(r'\bfor\s+(?:the\s+)?(?:next\s+|another\s+)?(\d{1,3}|a couple of|couple of|' + '|'.join(k for k in NUMBERS if ' ' not in k) + r')\s+(?:more\s+)?(day|night|week|month)s?\b', w)
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
            unit = m.group(2)
            if unit == 'month':
                mo = said.month - 1 + n
                y, mo = said.year + mo // 12, mo % 12 + 1
                found = date(y, mo, min(said.day, calendar.monthrange(y, mo)[1]))
            else:
                found = said + timedelta(days=n * (7 if unit == 'week' else 1))
    if found is None:
        if re.search(r'\b(?:this|all|the rest of the|rest of the|the whole|whole)\s+week\b', w):
            found = sunday
        elif re.search(r'\bthis\s+weekend\b', w):
            found = sunday
        elif re.search(r'\bnext\s+week\b', w):
            found = sunday + timedelta(days=7)
        elif re.search(r'\b(?:this|all|the rest of the|rest of the)\s+month\b', w):
            found = _month_end(said.year, said.month)
        elif re.search(r'\b(?:this|the rest of the|rest of the)\s+year\b', w):
            found = date(said.year, 12, 31)
        elif re.search(r'\b(?:today|tonight|this (?:morning|afternoon|evening))\b', w):
            found = said
        elif re.search(r'\btomorrow\b', w):
            found = said + timedelta(days=1)
    if found is None or found < said or (found - said).days > MAX_DAYS:
        return None
    return found


def plain_day(d: date, today: date | None = None) -> str:
    """'Friday Nov 20' (with the year only when it is not this one)."""
    text = d.strftime('%A %b ') + str(d.day)
    return text if today is None or d.year == today.year else text + ', %d' % d.year
