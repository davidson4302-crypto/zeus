"""Sourced renewal terms for Zeus. One JSON request and reply, using only the standard library."""
import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATE_FIELDS = ("charge_on", "renewal_on", "cancel_by")
TERM_FIELDS = (*DATE_FIELDS, "cutoff_boundary", "amount", "currency", "notice", "timezone")
NUMBER_WORDS = dict(enumerate("zero one two three four five six seven eight nine ten eleven twelve".split()))
NUMBER_WORDS.update({14: "fourteen", 15: "fifteen", 30: "thirty", 60: "sixty", 90: "ninety"})
UNIT_WORDS = {"days": "day", "weeks": "week", "months": "month", "years": "year", "hours": "hour",
              "business_days": "business day", "business days": "business day"}
ALLOWED_OPS = {"notice", "get", "list", "decide", "payment"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _settings():
    """The Zeus engine settings, when this skill runs from the package (or ZEUS_ENGINE_DIR points at it)."""
    for folder in (os.environ.get("ZEUS_ENGINE_DIR"), Path(__file__).resolve().parents[3] / "engine"):
        if folder and (Path(folder) / "settings.py").is_file():
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            import settings
            return settings
    return None


def default_store():
    """HERMES_HOME (the active profile) wins; else the engine's life-engine folder from settings."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]) / "workspace/life-engine/renewals/records.sqlite3"
    settings = _settings()
    require(settings is not None, "Set HERMES_HOME to the Zeus profile or pass --store.")
    return Path(settings.LIFE_HOME) / "renewals/records.sqlite3"




def text(value, name, limit=400):
    require(isinstance(value, str) and value.strip() and len(value) <= limit, f"Supply {name} (up to {limit} characters).")
    return value.strip()


def identifier(value, name):
    value = text(value, name, 400)
    require(not any(ord(ch) < 32 for ch in value), f"Invalid {name}.")
    return value


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canon(value).encode()).hexdigest()


def normalized(value):
    return re.sub(r"\s+", " ", value.replace("’", "'").lower()).strip()


def evidence(req):
    return {"source_id": identifier(req.get("source_id"), "source_id"), "excerpt": text(req.get("excerpt"), "the exact source excerpt", 5000)}


def date_pattern(day):
    # Match the full date in a source notice, never the date on which the email arrived by coincidence.
    y, m, d = day.year, day.month, day.day
    month = rf"(?:{calendar.month_name[m]}|{calendar.month_abbr[m]}\.?)"
    ordinal = rf"0?{d}(?:st|nd|rd|th)?"
    alternatives = [re.escape(day.isoformat()), rf"{month}\s+{ordinal},?\s+{y}", rf"{ordinal}\s+{month}\s+{y}",
                    rf"0?{m}/0?{d}/{y}"]
    return r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\d)"


def contains_date(excerpt, day):
    return bool(re.search(date_pattern(day), excerpt, re.I))


def localize(wall, zone):
    valid = []
    for fold in (0, 1):
        value = wall.replace(tzinfo=zone, fold=fold)
        round_trip = value.astimezone(dt.timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == wall and value.utcoffset() not in [item.utcoffset() for item in valid]:
            valid.append(value)
    require(len(valid) == 1, "Local clock time is ambiguous or missing at a daylight-saving change; use a sourced offset.")
    return valid[0]


def parse_moment(value, timezone=None):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.date.fromisoformat(value)
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?", value),
            "A date must use YYYY-MM-DD or a sourced ISO date and time.")
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    zone = ZoneInfo(timezone) if timezone else None
    if moment.tzinfo is None:
        require(zone, "A timed notice needs a source-backed timezone or UTC offset.")
        return localize(moment, zone)
    if zone is not None:
        local = moment.astimezone(zone)
        require(local.replace(tzinfo=None) == moment.replace(tzinfo=None), "Timezone disagrees with the supplied UTC offset.")
        return local
    return moment


def supported_clock(excerpt, moment):
    # An ISO stamp contains its own clock. Natural-language clocks may use AM/PM.
    h, m = moment.hour, moment.minute
    clock = rf"(?<!\d){h:02}:{m:02}(?!\d)"
    if re.search(clock, excerpt):
        return True
    hour12 = h % 12 or 12
    minute = rf":{m:02}" if m else r"(?::00)?"
    suffix = r"p\.?m\.?" if h >= 12 else r"a\.?m\.?"
    return re.search(rf"(?<!\d){hour12}{minute}\s*{suffix}(?![A-Za-z])", excerpt, re.I) is not None


def parse_date_field(value, timezone, excerpt, name):
    moment = parse_moment(text(value, name, 80), timezone)
    day = moment.date() if isinstance(moment, dt.datetime) else moment
    require(contains_date(excerpt, day), f"{name} date was absent from the quoted source.")
    if isinstance(moment, dt.datetime):
        require(supported_clock(excerpt, moment), f"{name} clock time was absent from the quoted source.")
        if not timezone:
            offset = moment.strftime("%z")
            offsets = {offset, offset[:3] + ":" + offset[3:]}
            utc = moment.utcoffset() == dt.timedelta(0) and re.search(r"\bUTC\b|\dZ\b|\bGMT\b", excerpt, re.I)
            require(utc or any(off in excerpt for off in offsets), f"{name} timezone was absent from the quoted source.")
    return moment.isoformat()


def clauses(excerpt):
    # Keep decimals and abbreviations intact. Semicolons/newlines typically separate unrelated policy facts.
    return [chunk.strip() for chunk in re.split(r"(?:[;\n]|(?<=[a-zA-Z0-9])\.\s+(?=[A-Z]))", excerpt) if chunk.strip()]


def field_excerpt(req, name, excerpt):
    quotes = req.get("field_quotes", {})
    require(isinstance(quotes, dict), "field_quotes must map field names to exact source quotations.")
    quote = quotes.get(name)
    if quote is None:
        return excerpt
    quote = text(quote, name+" source quotation", 1800)
    require(normalized(quote) in normalized(excerpt), name+" quote is absent from the source.")
    return quote


def money(req, excerpt):
    value, currency = req.get("amount"), req.get("currency")
    if value is None and currency is None:
        return {}
    amount = text(value, "amount as a decimal string", 32)
    require(re.fullmatch(r"\d{1,12}(?:\.\d{1,4})?", amount), "Invalid decimal amount.")
    if currency is None:
        matched = re.findall(r"(?:[A-Za-z]{3}|[$€£¥])\s*(\d+(?:\.\d+)?)", excerpt.replace(",", ""))
        require(any(Decimal(n) == Decimal(amount) for n in matched), "The quoted notice must show the amount.")
        return {"amount": format(Decimal(amount), "f")}
    code = text(currency, "ISO currency", 3).upper()
    require(re.fullmatch(r"[A-Z]{3}", code), "Invalid ISO currency.")
    plain = excerpt.replace(",", "")
    number = Decimal(amount)
    markers = {"USD": r"USD|US\$|\$", "EUR": r"EUR|€", "GBP": r"GBP|£", "JPY": r"JPY|JP¥|¥",
               "CAD": r"CAD|CA\$|C\$", "AUD": r"AUD|AU\$|A\$"}.get(code, re.escape(code))
    # A bare dollar sign supports only a currency explicitly identified elsewhere in the same excerpt.
    if code == "USD" and not re.search(r"(?:\bUSD\b|\bUS\$|U\.?S\.? dollars?)", excerpt, re.I):
        markers = r"USD|US\$"
    pattern = rf"(?:(?<![A-Za-z])(?:{markers})\s*(\d+(?:\.\d+)?)(?!\d|\.\d)|(?<![\d.])(\d+(?:\.\d+)?)\s*(?:{markers})(?![A-Za-z]))"
    require(any(Decimal(m.group(1) or m.group(2)) == number for m in re.finditer(pattern, plain, re.I)),
            "The excerpt must link this amount and currency. If only the currency is unknown, omit its code.")
    minor = {"JPY":0,"KRW":0,"KWD":3,"BHD":3}.get(code, 2)
    display = format(number, f".{max(minor,-number.normalize().as_tuple().exponent)}f")
    return {"amount": display, "currency": code}


def parse_notice_period(data, excerpt):
    require(isinstance(data, dict), "notice must contain count and unit.")
    count = data.get("count")
    require(type(count) is int and 0 <= count <= 10000, "Invalid notice count.")
    unit = text(data.get("unit"), "notice unit", 24).lower()
    if unit not in UNIT_WORDS:
        unit = unit + "s"
    require(unit in UNIT_WORDS, "Unknown notice period unit. Leave it unknown or use an explicit sourced cutoff.")
    word = UNIT_WORDS[unit]
    units = r"(?:(?:calendar)[\s-]+)?day" if word == "day" else word.replace(" ", r"[\s-]+")
    nums = [str(count)]
    if count in NUMBER_WORDS:
        nums.append(NUMBER_WORDS[count])
    period_pattern = r"(?<!\w)(?:" + "|".join(nums) + r")[\s-]+" + units + r"s?\b"
    linked = (period_pattern + r"(?:['’]s?)?\s+(?:(?:of|written|prior|advance|calendar)\s+)*(?:notice|before|prior to|ahead of|in advance)\b"
              + r"|(?:notice(?: period)?|advance notice|prior notice)\s*(?:of|is|:|=)?\s*(?:at least\s+)?"+period_pattern)
    require(re.search(linked, excerpt, re.I), "The duration must describe advance cancellation notice.")
    # A provider may measure notice from a renewal date that differs from the billing date.
    target = re.search(r"(?:before|prior to|ahead of|in advance of)\s+(?:(?:the|your|our|next|automatic|scheduled|annual|monthly)\s+){0,3}(charge|debit|bill(?:ing)?|renew(?:al|s)?)\b", excerpt, re.I)
    anchor = "renewal_on" if target and target[1].lower().startswith("renew") else "charge_on" if target else None
    given = data.get("anchor")
    if given:
        require(given in ("charge_on","renewal_on"), "Unknown notice period anchor.")
        require(anchor == given, "The quoted notice must support the selected anchor.")
    result = {"count": count, "unit": unit.replace(" ", "_")}
    if anchor:
        result["anchor"] = anchor
    return result


def previous_date(moment, period):
    count, unit = period["count"], period["unit"]
    if unit == "business_days":
        return None, "Business-day cutoff needs the provider's holiday rules or an explicit cancellation date."
    if unit == "hours":
        if not isinstance(moment, dt.datetime):
            return None, "An hourly notice needs the actual time and timezone of the event."
        return (moment.astimezone(dt.timezone.utc) - dt.timedelta(hours=count)).astimezone(moment.tzinfo), None
    if unit in ("months", "years"):
        months = count * (12 if unit == "years" else 1)
        year, month0 = divmod(moment.year * 12 + moment.month - 1 - months, 12)
        require(year >= 1, "Notice period predates the supported calendar.")
        date_value = moment.replace(year=year, month=month0+1, day=min(moment.day, calendar.monthrange(year, month0+1)[1]))
    else:
        date_value = moment - dt.timedelta(days=count * (7 if unit == "weeks" else 1))
    if isinstance(date_value, dt.datetime) and isinstance(date_value.tzinfo, ZoneInfo):
        try:
            date_value = localize(date_value.replace(tzinfo=None), date_value.tzinfo)
        except ValueError:
            return None, "Calculated local cutoff crosses an ambiguous or missing clock time; verify the provider's cutoff."
    return date_value, None


def notice_from(req):
    source = evidence(req)
    quote = source["excerpt"]
    vendor = text(req.get("vendor"), "vendor", 180)
    require(re.search(r"(?<!\w)"+re.escape(normalized(vendor))+r"(?!\w)", normalized(quote)),
            "The quoted source must name this provider. Include its verified original sender or account header if necessary.")
    fields = money(req, field_excerpt(req, "amount", quote))
    timezone = req.get("timezone")
    if timezone is not None:
        timezone = text(timezone, "IANA timezone", 80)
        ZoneInfo(timezone)
        # An account setting or the provider page can be included in the same selected evidence.
        regional = {"America/Los_Angeles": r"\bPacific(?: time)?\b|\bP[SD]T\b",
                    "America/New_York": r"\bEastern(?: time)?\b|\bE[SD]T\b",
                    "America/Chicago": r"\bCentral(?: time)?\b|\bC[SD]T\b",
                    "America/Denver": r"\bMountain(?: time)?\b|\bM[SD]T\b"}
        require(timezone.lower() in quote.lower() or re.search(regional.get(timezone, r"(?!)"), quote, re.I),
                "The quoted source must identify the proposed timezone.")
        fields["timezone"] = timezone
    for name in DATE_FIELDS:
        if req.get(name) is not None:
            source_text = field_excerpt(req, name, quote)
            fields[name] = parse_date_field(req[name], timezone, source_text, name)
            day = dt.date.fromisoformat(fields[name][:10])
            contexts = [c for c in clauses(source_text) if contains_date(c, day)]
            cues = {"charge_on": r"charg|bill|renew|debit|collect|payment|payable|deduct",
                    "renewal_on": r"renew|subscription period|new term|next term|anniversary",
                    "cancel_by": r"cancel|opt.out|notice|decline|end|terminat"}
            require(any(re.search(cues[name], c, re.I) for c in contexts), f"Quoted {name} date lacks its provider context.")
            if name == "cancel_by":
                related = " ".join(contexts)
                date_expr = date_pattern(day)
                require(not re.search(r"\b(?:hours?|days?|weeks?|months?|years?)\s+(?:before|prior to)\s+"+date_expr, related, re.I),
                        "That date anchors the advance notice; it is not an explicit cancellation cutoff.")
                exclusive = re.search(r"\b(?:before|prior to)\s+"+date_expr, related, re.I)
                inclusive = re.search(r"\bon or before\s+"+date_expr, related, re.I)
                fields["cutoff_boundary"] = "before" if exclusive and not inclusive else "by"
    if fields.get("charge_on") and not fields.get("renewal_on"):
        # A price attached to "renews on ..." can support the combined date. A standalone bill date cannot.
        charge_day = dt.date.fromisoformat(fields["charge_on"][:10])
        if any(contains_date(c, charge_day) and re.search(r"\brenew(?:s|al)?(?:\s+(?:date|on|is))?\s*[:]?\s*(?:\d|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", c, re.I)
               for c in clauses(field_excerpt(req, "charge_on", quote))):
            fields["renewal_on"] = fields["charge_on"]
    if req.get("notice") is not None:
        source_text = field_excerpt(req, "notice", quote)
        matches = []
        for c in clauses(source_text):
            if re.search(r"cancel|opt.out|notice|decline|terminat|non.renew", c, re.I):
                try:
                    matches.append(parse_notice_period(req["notice"], c))
                except ValueError:
                    pass
        require(matches, "The linked cancellation terms must show the period and unit.")
        require(len({canon(p) for p in matches}) == 1, "Competing notice-period anchors; retain an exact policy quote.")
        fields["notice"] = matches[0]
    terms = req.get("terms")
    if terms is not None:
        terms = text(terms, "quoted terms", 1800)
        require(normalized(terms) in normalized(quote), "Terms must be quoted from the supplied source.")
    supersedes = req.get("supersedes", [])
    require(isinstance(supersedes, list) and len(supersedes) == len(set(supersedes)), "supersedes must list distinct earlier source IDs.")
    supersedes = sorted(identifier(x, "superseded source ID") for x in supersedes)
    require(source["source_id"] not in supersedes, "A notice cannot supersede itself.")
    return {**source, "vendor": vendor, "fields": fields, "terms": terms, "supersedes": supersedes}


SCHEMA = """CREATE TABLE IF NOT EXISTS entries (
  id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(id, source)
)"""
def entries(db, key):
    return [json.loads(row[0]) for row in db.execute("SELECT payload FROM entries WHERE id=? ORDER BY rowid", (key,))]


def earlier(values):
    # Compare instants only when every candidate specifies an instant. With mixed granularities compare dates.
    values = list(filter(None, values))
    if not values:
        return None
    if all("T" in v for v in values):
        return min(values, key=lambda x: dt.datetime.fromisoformat(x).astimezone(dt.timezone.utc))
    return min(v[:10] for v in values)


def cutoff(fields):
    explicit = fields.get("cancel_by")
    if explicit and fields.get("cutoff_boundary") == "before" and "T" not in explicit:
        explicit = (dt.date.fromisoformat(explicit)-dt.timedelta(days=1)).isoformat()
    anchor_name = (fields.get("notice") or {}).get("anchor")
    # One supplied renewal/charge day is used by the common combined renewal-and-charge notice.
    # If both days are present and differ, the provider must say which anchors the period.
    renewal, charge = fields.get("renewal_on"), fields.get("charge_on")
    if renewal and charge and renewal != charge and not anchor_name and fields.get("notice") and not explicit:
        return None, "Notice does not specify whether the different renewal or billing date anchors the period.", None
    anchor = fields.get(anchor_name) if anchor_name else renewal or charge
    if anchor_name and not anchor and not explicit:
        return None, f"The notice uses {anchor_name}, but that date is missing from the source.", None
    suggested = None
    issue = None
    if fields.get("notice") and anchor:
        calculated, issue = previous_date(parse_moment(anchor, fields.get("timezone")), fields["notice"])
        suggested = calculated.isoformat() if calculated else None
    if explicit and suggested:
        # A date-only derived deadline and an exact provider cutoff on that same date agree.
        equal = (dt.datetime.fromisoformat(explicit) == dt.datetime.fromisoformat(suggested)
                 if "T" in explicit and "T" in suggested else explicit[:10] == suggested[:10])
        if not equal:
            return None, "The provider's explicit cutoff differs from the notice-period calculation.", earlier([explicit, suggested])
    value = explicit or suggested
    return value, None if value else issue, value


def view(key, rows):
    all_notices = [r for r in rows if r["kind"] == "notice"]
    require(all_notices, "Unknown renewal id.")
    replaced = {s for n in all_notices for s in n["supersedes"]}
    active = [n for n in all_notices if n["source_id"] not in replaced]
    values, conflicts, issues = {}, [], []
    for name in TERM_FIELDS:
        distinct = {canon(n["fields"][name]): n["fields"][name] for n in active if name in n["fields"]}
        if len(distinct) > 1:
            conflicts.append(name)
        elif distinct:
            values[name] = next(iter(distinct.values()))
    candidates = []
    for n in active:
        _, _, candidate = cutoff(n["fields"])
        candidates.append(candidate)
    actual_cutoff, problem, candidate = cutoff(values)
    candidates.append(candidate)
    if problem:
        issues.append(problem)
        if "differs" in problem:
            conflicts.append("cancel_by")
    if conflicts:
        actual_cutoff = None
    # Only an explicit notice supersedes a prior notice. A new source alone does not establish a correction.
    unique_terms = list(dict.fromkeys(n["terms"] for n in active if n["terms"]))
    if len(unique_terms) > 1:
        # Terms may simply repeat in different words. Never select silently between two substantive policies.
        if "terms" not in conflicts:
            conflicts.append("terms")
        actual_cutoff = None
    revision = digest({"vendor": active[0]["vendor"].casefold(), "values": values, "terms": sorted(unique_terms),
                       "conflicts": sorted(conflicts), "candidates": sorted(set(filter(None, candidates)))})
    decisions = [r for r in rows if r["kind"] == "decide"]
    decision = next((r for r in reversed(decisions) if r["revision"] == revision), None)
    payments = [r for r in rows if r["kind"] == "payment"]
    payment = payments[-1] if payments else None
    if not actual_cutoff and not issues and not conflicts:
        issues.append("Find the cancellation cutoff or the notice period and its anchor date in the provider's terms.")
    status = "conflict" if conflicts else "decided" if decision else "pending" if actual_cutoff else "needs_terms"
    record = {k: values.get(k) for k in TERM_FIELDS}
    due = earlier(candidates) if conflicts else actual_cutoff
    return {**record, "id": key, "vendor": active[0]["vendor"], "stated_cancel_by": values.get("cancel_by"), "cancel_by": actual_cutoff, "due_at": due,
            "status": status, "revision": revision, "terms": unique_terms, "issues": issues, "conflicts": conflicts,
            "source_ids": [n["source_id"] for n in active], "sources": [{k: n[k] for k in ("source_id","excerpt")} for n in active],
            "decision": decision, "prior_decisions": [r for r in decisions if r != decision], "payment": payment,
            "payments": payments, "updated_at": rows[-1]["recorded_at"], "action_key": "zeus-renewal:"+key}


def action_from(req, current):
    op = req["op"]
    src = evidence(req)
    require(req.get("revision") == current["revision"], "Read the current record and pass its revision before recording an outcome.")
    if op == "decide":
        require(not current["conflicts"], "Resolve conflicting notices before recording a decision.")
        choice = req.get("decision")
        require(choice in ("keep","cancel"), "Use decision keep or cancel with the owner's actual source.")
        raw = normalized(src["excerpt"])
        # Require a direct instruction or first-person decision. A provider policy, question or quoted option is not a choice.
        verbs = {"keep": r"keep|renew|retain|continue", "cancel": r"cancel|terminate|opt out"}
        raw = re.sub(r"\b(?:do not|don't|never)\s+cancel\b", "keep", raw)
        raw = re.sub(r"\b(?:do not|don't|never)\s+(?:renew|keep|continue)\b", "cancel", raw)
        require("?" not in raw and not re.search(r"\b(?:if|maybe|unless|might|possibly|unsure|no longer|rather than)\b", raw),
                "The owner's choice is unresolved.")
        prefix = (r"^(?:(?:yes|yep|yeah|ok|okay|please|definitely|go ahead and|let's|let us)[,.! ]+)*"
                  r"(?:(?:i (?:want to|would like to|decided to|choose to|will)|i'll|i'd like to)\s+)?")
        selected = re.match(prefix + r"(?:" + verbs[choice] + r")\b", raw)
        opposite = "cancel" if choice == "keep" else "keep"
        require(selected and not re.search(r"\b(?:" + verbs[opposite] + r")\b", raw),
                "The cited owner words do not establish the selected decision.")
        target = raw[selected.end():].strip(" .!,")
        meaningful_targets = r"^(?:it|this|that|the subscription|the service|the plan|my subscription|my plan|my service|for this cycle|for another year)\b"
        require(not target or normalized(current["vendor"]) in target or re.search(meaningful_targets,target),
                "The quoted instruction does not identify this renewal as its target.")
        return {**src, "decision": choice, "revision": current["revision"]}
    require(not current["conflicts"], "Reconcile this cycle's conflicting notices before linking a payment.")
    require(req.get("vendor") and normalized(text(req["vendor"], "vendor")) == normalized(current["vendor"]), "Payment vendor does not match this renewal.")
    require(normalized(current["vendor"]) in normalized(src["excerpt"]), "The receipt must identify this provider.")
    amount = money(req, src["excerpt"])
    require(amount.get("currency"), "Supply the charged amount and currency shown on the provider receipt.")
    require((current["amount"] is None or Decimal(amount["amount"]) == Decimal(current["amount"])) and (current["currency"] is None or amount["currency"] == current["currency"]),
            "Receipt amount or currency differs from the renewal. Reconcile the provider record before posting payment.")
    receipt = src["excerpt"]
    paid_on = parse_date_field(req.get("paid_on"), req.get("timezone"), receipt, "paid_on")
    day = dt.date.fromisoformat(paid_on[:10])
    positive = (r"\b(?:payment (?:received|successful|completed|confirmed)|(?:successfully|was|were|has been) (?:paid|charged)|"
                r"(?:total|amount|status)\s*:?\s*(?:paid|charged)|(?:paid|charged)\s+(?:on\s+|[A-Z]{3}\s*\d|[$€£¥]\s*\d|\d+))\b")
    contrary = (r"\b(?:will|may|to|scheduled to|expected to|not|never|hasn't been|has not been) (?:fully |yet |be )?(?:charged|paid|collected|received)\b|"
                r"\bunpaid\b|\b(?:refunded|reversed|declined|pending|no payment received|outstanding balance|past due)\b")
    require(not re.search(contrary, normalized(receipt)), "The source describes an uncertain, unpaid, future or reversed charge.")
    paid_clauses = [c for c in clauses(receipt) if re.search(positive, c, re.I) and contains_date(c, day)]
    matched_amount = False
    for c in paid_clauses:
        try:
            money(req, c)
            matched_amount = True
        except ValueError:
            continue
    if not matched_amount and not re.search(r"\b(?:previous|prior|next|upcoming|balance due|amount due)\b", receipt, re.I):
        # Itemized receipts often split the successful status, actual payment date and actual paid amount into labeled lines.
        lines = [line.strip() for line in receipt.splitlines() if line.strip()]
        has_status = any(re.search(r"\bpayment (?:received|successful|completed|confirmed)\b|\bstatus\s*:\s*paid\b", line, re.I) for line in lines)
        paid_dates = [line for line in lines if re.search(r"^\s*(?:payment date|paid on|date paid)\s*:",line,re.I)]
        paid_totals = [line for line in lines if re.search(r"^\s*(?:amount paid|total paid|amount charged)\s*:",line,re.I)]
        if has_status and len(paid_dates) == len(paid_totals) == 1 and contains_date(paid_dates[0], day):
            try:
                money(req, paid_totals[0])
                matched_amount = True
            except ValueError:
                pass
    require(matched_amount, "The provider receipt must link payment, the actual paid amount, and the payment date.")
    cycle = (current.get("charge_on") or current.get("renewal_on") or "")[:10]
    require(cycle, "The renewal needs a sourced cycle date before attaching a provider payment.")
    if paid_on[:10] != cycle:
        require(req.get("applies_to") == cycle and contains_date(src["excerpt"], dt.date.fromisoformat(cycle)),
                "A receipt from a different day must explicitly identify this cycle in its source.")
    return {**src, **amount, "applies_to":cycle, "vendor": current["vendor"], "paid_on": paid_on, "status": "verified", "revision": current["revision"]}


def write(db, key, req):
    rows = entries(db, key)
    if req["op"] == "notice":
        values = notice_from(req)
        prior = {r["source_id"]:r for r in rows if r["kind"] == "notice"}
        require(all(s in prior for s in values["supersedes"]), "A superseded source is missing from this renewal.")
        if prior:
            require(normalized(next(iter(prior.values()))["vendor"]) == normalized(values["vendor"]), "Renewal ID already belongs to another provider.")
    else:
        # A repeated outcome remains idempotent even if a later notice changed the terms.
        old = next((r for r in rows if r["source_id"] == req.get("source_id") and r["kind"] == req["op"]),None)
        if old and old["request_hash"] == digest(req):
            return view(key, rows)
        values = action_from(req, view(key, rows))
    data = {**values,"kind":req["op"]}
    if req["op"] != "notice":
        data["request_hash"] = digest(req)
    exists = next((r for r in rows if r["source_id"] == data["source_id"]),None)
    if exists:
        require({k:v for k,v in exists.items() if k != "recorded_at"} == data, "Source ID already has different content. Keep it and add a new sourced correction.")
        return view(key, rows)
    data["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    db.execute("INSERT INTO entries VALUES (?, ?, ?, ?)", (key,data["source_id"],data["kind"],canon(data)))
    return view(key, rows+[data])


def execute(store, req):
    require(isinstance(req, dict) and req.get("op") in ALLOWED_OPS, "Use notice, get, list, decide, or payment.")
    op = req["op"]
    key = None if op == "list" else identifier(req.get("id"), "stable subscription-cycle id")
    read_only = op in ("list","get")
    through = req.get("through")
    if through is not None:
        require(op == "list" and isinstance(through, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", through), "through must be YYYY-MM-DD.")
        dt.date.fromisoformat(through)
    if read_only and not store.exists():
        require(op == "list", "Unknown renewal id.")
        return {"records":[]}
    if read_only:
        db = sqlite3.connect(store.resolve().as_uri()+"?mode=ro", uri=True, timeout=10)
    else:
        store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(store, timeout=10)
    try:
        if read_only:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'").fetchone():
                require(op == "list", "Unknown renewal id.")
                return {"records":[]}
            if op == "get":
                result = {"record": view(key, entries(db,key))}
            else:
                records = [view(k, entries(db,k)) for k, in db.execute("SELECT DISTINCT id FROM entries ORDER BY id")]
                if through:
                    records = [r for r in records if r["status"] != "decided" and (r["due_at"] is None or r["due_at"][:10] <= through)]
                result = {"records": sorted(records,key=lambda r:(r["due_at"] is not None,r["due_at"] or "",r["id"]))}
        else:
            # A rejected first notice still leaves an initialized, empty store for the hourly reader.
            db.execute(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            result = {"record": write(db,key,req)}
        db.commit()
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, help="Explicit SQLite file, e.g. for an isolated profile.")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        store = args.store or default_store()
        result = execute(store, json.load(sys.stdin))
    except (ValueError, TypeError, KeyError, OverflowError, OSError, InvalidOperation, ZoneInfoNotFoundError, sqlite3.Error) as error:
        print(json.dumps({"ok":False, "error":str(error)}))
        return 1
    print(json.dumps({"ok":True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
