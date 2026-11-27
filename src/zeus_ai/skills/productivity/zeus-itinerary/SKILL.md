---
name: zeus-itinerary
description: "Use when the owner wants a trip itinerary, trip packet, travel calendar file (.ics), portable/shareable itinerary, hotel or flight legs gathered in one document, or a correction or re-export of an existing trip."
---

# Make or update a portable trip itinerary

Save the source-backed trip once. Give the owner a written itinerary and an importable iCalendar file when requested. This workflow creates local files. Adding these items to a calendar is a separate change that needs the owner's yes for each item (see zeus-calendar). Sending these files to anyone else requires a separate instruction for that destination.

1. Read the supplied itinerary, confirmations or owner's correction. Record source, known dates, local times and time zones. Keep separate stable trip and item IDs (for example, `weekend-trip`, `outbound`, `hotel`, `pickup`). A change of flight number, date or hotel updates the same item. Use distinct IDs for each leg, even on the same day. If sources disagree, identify the exact conflict and ask which source prevails. Keep confirmed unrelated items.
2. Use this skill's `scripts/itinerary.py` through the terminal. Resolve it relative to the installed skill location returned by Hermes, normally `$HERMES_HOME/skills/productivity/zeus-itinerary/scripts/itinerary.py`. Use Zeus's profile home. With no `HERMES_HOME`, pass an explicit `--state-dir` under the verified Zeus profile. For a demonstration or a test, always pass a temporary state directory. Do not call providers or book anything to make a file.
3. Use `show TRIP_ID` for the last saved revision. A new trip starts at revision 0. Use `save --input -` with the JSON below on standard input. Save one source per request. `source_ref` is a private source handle or the ID of the owner's correction. Never put a booking reference, ticket, password, card number, raw message, address, passenger name or phone number into either IDs or public fields. Store freeform material only in its original source. IDs are technical labels.
4. Attribute every supplied field to the source. Only include a public venue, airport or station name in `public_place`. A known personal pickup, home or contact stays out of the shareable fields. For vague timing, preserve a known date only or omit the unknown time/date; avoid turning “evening” into a guessed hour. A fact with no date can still appear in the written itinerary. For a timed event use ISO local time with accurate numeric offset, or an unambiguous IANA zone (e.g. America/Los_Angeles). Use both when available. A repeated clock hour during the autumn daylight-saving transition needs the source's offset. Date-only ranges have an exclusive end: a hotel stay from Saturday to Monday checks out Monday. Keep every leg's origin and destination zones separate. If a dated milestone and a precise time have different precision, keep the two known milestones separately and label them accurately.
5. On correction include the existing item ID, latest `base_revision`, corrected field(s), the new source reference and `"confirm_correction": true` only when the source explicitly supersedes the earlier value or the owner has resolved the conflict. A second incompatible source on its own cannot replace recorded facts. Omitted fields and other items survive. Use JSON `null` only to explicitly clear an optional fact. `remove_ids` records an explicit cancellation. If the helper reports a stale revision, read the current trip and reconcile the source before saving. It rejects unrecognized fields. A merge must not silently replace an unrelated trip or a competing correction.
6. Run `export TRIP_ID --out-dir /absolute/output/folder`. Default to a user-accessible local Zeus output folder when the owner asks for files without choosing a directory. The JSON result contains the `markdown` and `ics` paths along with event and item counts. Read the files back, confirm legs/dates against the sources and share both files through the current surface's supported attachment or local-link mechanism. For terminal or shared CLI delivery, paste the export's ready-to-send `owner_handoff` in the final answer. Keep an undated item's timing explicitly unknown. Never claim a file reached a phone until that surface confirms it. No calendar import is part of an export.
7. Each revision gets its own directory. Re-exporting the same revision verifies both existing files. If someone edited a file, choose a fresh output directory. Newer snapshots preserve existing calendar event IDs and omit explicitly cancelled items. Import behavior belongs to the calendar app; someone who previously imported may need to remove a cancelled event there.

From the installed skill directory, example with synthetic data:

```sh
python3 scripts/itinerary.py save --input - <<'JSON'
{"trip_id":"weekend-trip","base_revision":0,"source_ref":"owner-message-42","items":[{"id":"outbound","kind":"flight","origin":"LAX","destination":"ORD","flight_number":"UA123","start":"2027-03-12T23:30:00-08:00","start_zone":"America/Los_Angeles","end":"2027-03-13T05:30:00-06:00","end_zone":"America/Chicago"},{"id":"hotel","kind":"hotel","public_place":"Grand Hotel","start":"2027-03-13","end":"2027-03-15"},{"id":"pickup","kind":"pickup","public_place":"Terminal Two"}]}
JSON
python3 scripts/itinerary.py show weekend-trip
python3 scripts/itinerary.py export weekend-trip --out-dir "$HERMES_HOME/outputs/itineraries"
```

Supported kinds are `flight`, `hotel`, `arrival`, `pickup` and `event`. All trip times and places come from the source. The helper accepts structured data only, stores facts and revisions privately, and exports only the approved public fields. Pass `--state-dir DIRECTORY` before the subcommand when overriding state.

## Delivery

After checking the files, paste `owner_handoff` verbatim into the terminal or shared CLI reply, including when reporting several tasks together. This is the delivery receipt: it contains the verified folder, both filenames, and a local link for each. Rewriting it as just two absolute file paths makes the written itinerary disappear from the terminal. On other surfaces use their normal attachment mechanism.
