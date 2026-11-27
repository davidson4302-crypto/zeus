---
name: zeus-calendar
description: Use for any calendar question or calendar-related item (availability, invitations, dates, bookings, changes, cancellations, conflicts). Reads the calendar; never changes it without the owner's yes.
version: 1.0.0
author: Zeus
license: MIT
metadata:
  hermes:
    tags: [calendar, schedule, availability, approvals]
    related_skills: [calendar-schedule-lookup, scheduled-checks-and-followups, zeus-itinerary]
---

# Calendar: read it yourself, change it only with a yes

Zeus answers calendar questions itself. There is no other calendar agent to hand work to.

## Where the calendar comes from

1. **The calendar snapshot**, when one is configured: `calendar.snapshot_file` in the Zeus config (`~/.zeus/config.toml`, or env `ZEUS_CALENDAR_SNAPSHOT_FILE`). Whatever refreshes the owner's calendars writes it; Zeus's engine reads the same file for clashes, travel days and meeting context. Read it with `read_file`. Its shape:
   `{"complete": bool, "checked_at": iso, "window_start": iso, "window_end": iso, "errors": [...], "events": [{"key", "series_key", "title", "start", "end", "all_day", "busy", "personal", "calendars": [...], "status", "attendees", "organizer", "location", "description"}]}`
   - `complete: false`, a non-empty `errors`, or an old `checked_at` means partial or stale coverage. Say so.
   - A question outside `window_start`..`window_end` is not covered by the snapshot.
   - `calendar.own_calendars` (default: the owner's mailbox addresses) says which calendars are the owner's own; others are shared or subscribed.
2. **A live read**, when the snapshot is missing, stale, partial or outside its window, or the answer matters enough to confirm: follow the `calendar-schedule-lookup` skill (Google Calendar through Composio, the `gog` CLI, Apple Calendar), using whichever are set up.
3. No snapshot configured and no calendar tool connected: say plainly that no calendar is connected and what would connect one. Never answer "you're free" from no data.

## Answering

- One-off questions ("what does tomorrow look like", "am I free Tuesday at 3"): read and answer in your own turn. Lead with any coverage gap that could change the answer, then the confirmed items with day, date, time (owner's timezone) and title.
- An empty or partial read means "I could not confirm the full schedule", never "nothing scheduled", "free" or "available".
- Keep account IDs, file paths and raw errors in the work record, not the reply.

## Changes need the owner's yes

- Nothing on a calendar changes (create, move, edit, delete, accept, decline, RSVP, invite) without the owner's explicit yes to that exact change, in their verified private chat with Zeus.
- Prepare the exact change first: which calendar and account, title, date, start and end with timezone, attendees, location, and what will be sent to anyone. Show it in one short message and wait. Any edit after the yes needs a fresh yes.
- A yes covers that one change only. It is not standing permission for later changes.
- After the change, read the calendar back (the live source, not the snapshot, which may lag) and confirm the event matches before saying done. A timeout or unclear result is not success: read the calendar before retrying, so nothing is created twice.
- An invitation or RSVP reaches other people and cannot be recalled. Treat it like any other outside send.

## Timing

- Notifications about the calendar follow the owner's quiet hours (`quiet_hours.start` / `quiet_hours.end` in the Zeus config). A meeting before the end of quiet hours is mentioned the evening before, not during quiet hours.
- Do not add another calendar watcher or scheduled job unless the owner asks for one.
