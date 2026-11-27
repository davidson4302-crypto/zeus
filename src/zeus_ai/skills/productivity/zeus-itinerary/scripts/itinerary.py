#!/usr/bin/env python3
"""Store source-backed trip facts and export portable, local-only snapshots."""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
KINDS = {"flight": "Flight", "hotel": "Hotel stay", "arrival": "Arrival", "pickup": "Pickup", "event": "Event"}
ITEM_KEYS = {"id", "kind", "start", "end", "start_zone", "end_zone", "origin", "destination", "flight_number", "public_place"}
PRIVATE_LABELS = re.compile(
    r"\b(?:confirmation|conf|booking|reservation (?:ref|no|id|code|number)|reference|ref|locator|pnr|ticket|guest name|passenger|travell?er|passport|password|token|secret|"
    r"receipt|voucher|credit card|loyalty|reservation (?:number|id|code)|pin code|door code|access code|"
    r"my home|home address|apartment|private address)\b", re.I)
STREET_ADDRESS = re.compile(r"\b\d+\s+.{0,48}\b(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|boulevard|blvd|court|ct|way)\b", re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value), "Use a short stable trip or item ID.")
    return value


def clean_place(value):
    require(isinstance(value, str) and 0 < len(value.strip()) <= 100, "Use a short public place name.")
    value = unicodedata.normalize("NFC", value.strip())
    safe_chars = all(c.isalpha() or c.isdecimal() or unicodedata.category(c).startswith("M") or c in " -'’.,&()・" for c in value)
    opaque = re.search(r"\b(?=[A-Za-z0-9]{6,}\b)(?=[A-Za-z0-9]*[a-zA-Z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+\b|\b[A-Z]{6,}\b", value)
    grouped_locator = re.search(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]{2,4}-[A-Z0-9]{2,4}\b", value)
    digits = sum(c.isdecimal() for c in value)
    require(safe_chars and not PRIVATE_LABELS.search(value) and not STREET_ADDRESS.search(value) and not opaque and not grouped_locator and digits < 5,
            "Place must be a public venue, airport or station name only. Omit private details.")
    return value


def instant(value, zone_name=None):
    """Return a date or explicitly localized datetime; reject DST gaps, ambiguous local times and contradictory offsets."""
    if value is None:
        require(zone_name is None, "A time zone needs a time.")
        return None
    require(isinstance(value, str), "Use an ISO date or timestamp.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        require(zone_name is None, "Date-only items have no time zone.")
        return date.fromisoformat(value)
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", value), "Use a date or timestamp with an offset or a named zone.")
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    zone = None
    if zone_name is not None:
        require(isinstance(zone_name, str) and len(zone_name) <= 64, "Invalid time zone.")
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unrecognized time zone.") from None
    if point.tzinfo:
        if zone:
            local = point.astimezone(zone)
            require(local.replace(tzinfo=None) == point.replace(tzinfo=None), "The stated local time, offset and zone disagree.")
            return local
        return point
    require(zone, "Clock times need a source-backed offset or IANA time zone.")
    candidates = {}
    for fold in (0, 1):
        local = point.replace(tzinfo=zone, fold=fold)
        absolute = local.astimezone(UTC)
        if absolute.astimezone(zone).replace(tzinfo=None) == point:
            candidates[absolute] = local
    require(len(candidates) == 1, "Ambiguous or nonexistent local time. Obtain the exact source offset.")
    return next(iter(candidates.values()))


def item_record(item):
    require(isinstance(item, dict) and set(item) <= ITEM_KEYS, "Unknown item field. Supply only share-safe structured facts.")
    item = {key: value for key, value in item.items() if value is not None}
    identifier(item.get("id"))
    kind = item.get("kind")
    require(kind in KINDS, "Unknown item kind.")
    for key in ("origin", "destination"):
        if key in item:
            require(kind == "flight" and isinstance(item[key], str) and re.fullmatch(r"[A-Z]{3}", item[key]), "Use a three-letter airport code on flight items.")
    if "flight_number" in item:
        value = item["flight_number"]
        require(kind == "flight" and isinstance(value, str) and re.fullmatch(r"(?:[A-Z][A-Z0-9]|[0-9][A-Z]) ?[0-9]{1,4}[A-Z]?", value), "Use a flight designator only.")
    if "public_place" in item:
        item["public_place"] = clean_place(item["public_place"])
    start = instant(item.get("start"), item.get("start_zone"))
    end = instant(item.get("end"), item.get("end_zone"))
    if start and end:
        require(type(start) == type(end), "An event range must use the same date precision at each end. Split differently sourced milestones.")
        if isinstance(start, datetime):
            require(end.astimezone(UTC) > start.astimezone(UTC), "The end must be after the start.")
        else:
            require(end > start, "The all-day end is the exclusive ending date.")
    return item


def normalize(request):
    require(isinstance(request, dict) and set(request) <= {"trip_id","base_revision","source_ref","items","remove_ids","confirm_correction"}, "Invalid request fields.")
    trip = identifier(request.get("trip_id"))
    base = request.get("base_revision")
    require(type(base) == int and base >= 0, "Supply the known base_revision, starting at 0.")
    source = request.get("source_ref")
    require(isinstance(source, str) and 0 < len(source.strip()) <= 250 and source.isprintable(), "Supply a short, private reference to the actual source.")
    entries = request.get("items", [])
    removals = request.get("remove_ids", [])
    require(isinstance(entries, list) and isinstance(removals, list) and 0 < len(entries) + len(removals) <= 150, "Supply 1 to 150 item changes.")
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) <= ITEM_KEYS, "Unknown item field. Supply only share-safe structured facts.")
        identifier(entry.get("id"))
    removals = [identifier(entry) for entry in removals]
    ids = [entry["id"] for entry in entries] + removals
    require(len(ids) == len(set(ids)), "Each item can change only once in a request.")
    confirmed = request.get("confirm_correction", False)
    require(type(confirmed) is bool, "confirm_correction must be a boolean.")
    return trip, base, source.strip(), entries, removals, confirmed


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save(directory, request):
    trip, base, source, entries, removals, confirmed = normalize(request)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.closing(sqlite3.connect(directory / "itineraries.sqlite", timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS revisions(trip TEXT NOT NULL, rev INTEGER NOT NULL, input_digest TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(trip,rev))")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT rev,input_digest,snapshot FROM revisions WHERE trip=? ORDER BY rev DESC LIMIT 1", (trip,)).fetchone()
        rev, digest, data = row if row else (0, None, '{"items":{}}')
        submitted = hashlib.sha256(encode(request).encode()).hexdigest()
        if base + 1 == rev and digest == submitted:
            return {"trip_id": trip, "revision": rev, "replayed": True}
        require(base == rev, "Stale base_revision. Read the latest trip, reconcile the correction and save again.")
        state = json.loads(data)
        records = state["items"]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for key in removals:
            require(key in records, "Cannot remove an item absent from the current trip.")
            del records[key]
        for patch in entries:
            prior = records.get(patch["id"], {})
            previous = prior.get("data", {})
            item = item_record({**previous, **patch})
            conflicting = sorted(key for key in patch if key in previous and previous[key] != item.get(key))
            if conflicting and not confirmed:
                raise ValueError("Source conflict for "+", ".join(conflicting)+". Keep the prior facts until an explicit correction resolves this.")
            if previous == item:
                continue
            field_sources = dict(prior.get("sources", {}))
            for field in patch:
                if field not in item:
                    field_sources.pop(field, None)
                elif field != "id" and previous.get(field) != item[field]:
                    field_sources[field] = source
            records[item["id"]] = {"data": item, "sources": field_sources, "revision": rev+1, "stamp": stamp}
        state.update({"source_ref": source, "removed": removals, "stamp": stamp})
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (trip, rev+1, submitted, encode(state)))
        return {"trip_id": trip, "revision": rev+1, "items": len(records)}


def read(directory, trip):
    identifier(trip)
    path = directory / "itineraries.sqlite"
    require(path.is_file(), "Trip does not exist.")
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
        row = db.execute("SELECT rev,snapshot FROM revisions WHERE trip=? ORDER BY rev DESC LIMIT 1", (trip,)).fetchone()
    require(row, "Trip does not exist.")
    snapshot = json.loads(row[1])
    return row[0], snapshot["items"], snapshot["stamp"]


def describe(item):
    title = KINDS[item["kind"]]
    if item.get("flight_number"):
        title += " " + item["flight_number"]
    if item.get("origin") or item.get("destination"):
        title += " " + item.get("origin", "?") + " → " + item.get("destination", "?")
    if item.get("public_place"):
        title += " · " + item["public_place"]
    return title


def display(point, zone):
    if isinstance(point, datetime):
        return point.isoformat(" ", timespec="seconds" if point.second else "minutes") + (" ("+zone+")" if zone else "")
    if point:
        return point.isoformat()
    return "Unknown"


def calendar_time(key, point):
    if isinstance(point, datetime):
        return key + ":" + point.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return key + ";VALUE=DATE:" + point.strftime("%Y%m%d")


def fold_ics(value):
    lines = [bytearray()]
    for char in value:
        raw = char.encode("utf-8")
        if len(lines[-1]) + len(raw) > 75:
            lines.append(bytearray(b" "))
        lines[-1].extend(raw)
    return b"\r\n".join(lines)


def sort_key(record):
    item = record["data"]
    start = instant(item.get("start"), item.get("start_zone"))
    if isinstance(start, datetime):
        return (0, start.astimezone(UTC).isoformat(), item["id"])
    return (0 if start else 1, start.isoformat() if start else "", item["id"])


def render(trip, revision, records, stamp):
    md = ["# Trip itinerary", "", f"Revision {revision}. Times below use the recorded local time and offset.", ""]
    cal = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Zeus//Trip itinerary//EN", "CALSCALE:GREGORIAN"]
    skipped = 0
    for record in sorted(records.values(), key=sort_key):
        item = record["data"]
        label = describe(item)
        start = instant(item.get("start"), item.get("start_zone"))
        end = instant(item.get("end"), item.get("end_zone"))
        md.extend(["## " + label, ""])
        md.append("- Start: " + display(start, item.get("start_zone")))
        end_description = "End (exclusive)" if type(end) is date else "End"
        md.extend(["- " + end_description + ": " + display(end, item.get("end_zone")), ""])
        if start is None:
            md.extend(["Start date unknown. This item appears only in the written itinerary.", ""])
            skipped += 1
            continue
        uid = hashlib.sha256((trip+"\0"+item["id"]).encode()).hexdigest()+"@zeus-itinerary.local"
        title = label.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
        cal.extend(["BEGIN:VEVENT", "UID:"+uid, "DTSTAMP:"+record["stamp"], "SEQUENCE:"+str(record["revision"]), "SUMMARY:"+title, calendar_time("DTSTART",start)])
        if end:
            cal.append(calendar_time("DTEND", end))
        cal.append("END:VEVENT")
    if not records:
        md.extend(["No current trip items.", ""])
    if skipped == len(records):
        # RFC 5545 requires a component. A journal preserves an empty calendar without inventing a date or event.
        journal_uid = hashlib.sha256((trip+"\0empty").encode()).hexdigest()+"@zeus-itinerary.local"
        cal.extend(["BEGIN:VJOURNAL", "UID:"+journal_uid, "DTSTAMP:"+stamp,
                    "SUMMARY:No dated itinerary items", "END:VJOURNAL"])
    cal.append("END:VCALENDAR")
    return {"Trip-Reference.md": ("\n".join(md).rstrip()+"\n").encode(), "Trip.ics": b"\r\n".join(fold_ics(line) for line in cal)+b"\r\n"}, skipped


def export(directory, trip, root):
    rev, records, stamp = read(directory, trip)
    files, skipped = render(trip, rev, records, stamp)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Private trip IDs never appear in the shared files or their directory name.
    folder_id = hashlib.sha256(trip.encode()).hexdigest()[:16]
    target = root / ("Trip-"+folder_id+"-r"+str(rev))
    if not os.path.lexists(target):
        staging = Path(tempfile.mkdtemp(prefix=".trip-", dir=root))
        try:
            for name, contents in files.items():
                (staging / name).write_bytes(contents)
            try:
                staging.rename(target)
            except OSError:
                if not os.path.lexists(target):
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    require(target.is_dir() and not target.is_symlink(), "The export path is occupied.")
    for name, contents in files.items():
        path = target / name
        require(not path.is_symlink() and path.is_file() and path.read_bytes() == contents, "Export contents differ. Existing files were preserved. Choose another output directory.")
    folder = target.absolute()
    markdown, ics = folder / "Trip-Reference.md", folder / "Trip.ics"
    # A local terminal can remove the absolute Markdown path during attachment extraction.
    # Plain filenames and the containing directory still identify both verified artifacts.
    qualified = [f"Folder on this Mac: {folder}"]
    for label, path in (("Written itinerary", markdown), ("Calendar file", ics)):
        qualified.append(f"{label}: {path.name} ([Open](<{quote(str(path), safe='/')}>))")
    return {"trip_id":trip, "revision":rev, "markdown":str(markdown), "ics":str(ics),
            "calendar_items":len(records)-skipped, "written_items":len(records), "undated_items":skipped,
            "owner_handoff":"\n".join(qualified)}


def default_directory():
    home = os.environ.get("HERMES_HOME")
    require(home and Path(home).is_absolute(), "Set --state-dir or a verified absolute HERMES_HOME for this profile.")
    return Path(home) / "state" / "zeus-itinerary"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("save").add_argument("--input", required=True)
    commands.add_parser("show").add_argument("trip_id")
    cmd = commands.add_parser("export")
    cmd.add_argument("trip_id")
    cmd.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    directory = args.state_dir or default_directory()
    if args.command == "save":
        stream = sys.stdin if args.input == "-" else Path(args.input).open(encoding="utf-8")
        with contextlib.nullcontext(stream) if args.input == "-" else stream:
            result = save(directory, json.load(stream))
    elif args.command == "show":
        rev, records, _ = read(directory, args.trip_id)
        result = {"trip_id":args.trip_id, "revision":rev, "items":[r["data"] for r in sorted(records.values(), key=sort_key)]}
    else:
        result = export(directory, args.trip_id, args.out_dir)
    print(encode(result))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(encode({"error":str(exc)}), file=sys.stderr)
        raise SystemExit(1)
