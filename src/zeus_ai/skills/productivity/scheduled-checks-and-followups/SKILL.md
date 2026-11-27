---
name: scheduled-checks-and-followups
description: Use when asked for a timed check or reminder, or to track a flight.
version: 1.1.1
author: Zeus
license: MIT
metadata:
  hermes:
    tags: [scheduling, followup, reminders, goals, cron]
    related_skills: [company-records]
---

# Scheduled checks and follow-ups

## When to Use

Use this when someone asks you to check, remind, or look again at a set time later. Covers requests like "check at 11:59 whether X exists", "remind me at 3", or "look again tomorrow morning".
Core rule: schedule it, or name the exact missing capability. Never hand the job back to the user ("message me at 11:59 and I'll check" is a handback, and it is not acceptable).

## Procedure

0. **Missing time:** if the request gives no clock time ("remind me tomorrow"), ask for it in one short question, offering a default like 9 AM. Use the owner's configured timezone (`owner.timezone` in the Zeus config) and don't ask for the zone. A watch on a known event (flight, pickup, appointment) is not this case. For a native flight watch, describe the events that trigger a text; give exact check times only for explicitly timed checks. See Travel and pickup watches. If the target is ambiguous (e.g. an odd product name that looks like a typo), confirm it in the same question, since the reminder text gets posted verbatim.
1. **Retain the goal first** with `zeus_work` `create`: the exact original request, the full path or target, the time and timezone, and the expected output. Reuse the same work item on later turns and save progress with `checkpoint`. Exception: a plain reminder to the owner ("remind me tomorrow to do X") needs no work item. Skip straight to the reminder or cron job and let its readback be the record.
2. **Run one live check now** (for a plain reminder, just run the script once by hand to confirm it prints the reminder line) (`date '+%H:%M:%S %Z'` plus an existence test on the full path) to get a baseline. A negative result now never cancels the later check.
3. **Decide where the result must land.** Use the channel the owner asked for, even when it differs from the request's origin. Default to the originating chat only if it supports delivery. For "text me", verify that the active profile's connected phone channel points at the owner's private thread before scheduling; an `origin` or local terminal job cannot text them.
   - To post in chat, use a one-time Hermes cron job with chat delivery. The recipe and a script template are in `references/hermes-cron-chat-check.md`.
4. **Schedule.** A plain chat reminder uses the same recipe: a `--no-agent` script that just `echo`s the reminder text (say what to do and what part needs the owner, e.g. a login), `--deliver <platform>:<chat_id>` from the message origin, cron expression `M H D MON *` with `--repeat 1`, and `HERMES_HOME` set to the active profile. For a deterministic check (file exists, value equals X), use `--no-agent` with a small script that prints exactly one chat-ready line. Script stdout is posted verbatim, and empty stdout posts nothing.
5. **Read the schedule back independently.** Read `$HERMES_HOME/cron/jobs.json` for the job id and confirm `enabled=true`, `state=scheduled`, the exact `next_run_at` with its offset, and the `deliver` target. Also run `hermes cron status` (the gateway is running) and `hermes cron doctor`. Save the job id and the readback on the work item's checkpoint.
6. **Reply:** give the useful baseline and the exact check times, time zone, and delivery channel. Say what's scheduled, not what's been delivered. Keep job IDs in the checkpoint, not in the owner's ordinary reply.
7. **After the run time:** read `last_run_at`, `last_status`, and `last_delivery_error` in jobs.json before calling the work complete.

## Apple Reminders (optional)

On a Mac, reminders can reach the owner's iPhone through Apple Reminders and iCloud (read, create and verify recipe: `references/apple-reminders.md`). Once the owner confirms a test reminder reached their phone and says to use it, default to it for "remind me to X at T" (plus a chat post only if they want one too). Until confirmed, say so and use the cron recipe.

Create it in list "Reminders" via osascript with both `due date` and `remind me date` (build the date by setting year/month/day/time on `current date`; times are in the owner's timezone, which should be the Mac's local zone). Bound it with `perl -e 'alarm 60; exec @ARGV' osascript ...`. Read it back with a fresh `whose name is "..."` query (count, due, alert, completed) before saying it's set. If the create call times out, re-query before trying again, so you don't end up with duplicates. Ticking off or deleting the owner's reminders counts as deleting, so ask first.

## Travel and pickup watches

1. Resolve the dated itinerary and verify the owner's private text destination. Check `zeus-life flight list` for an existing matching watch. If none exists, register it once with `zeus-life flight add --leg FLIGHT:YYYY-MM-DD:FROM:TO` (repeat `--leg` in travel order), `--who`, `--pickup AIRPORT`, `--from ORIGIN` when supported by evidence, and `--asked layover` when requested. Label a prior home origin as an assumption for today's drive.
2. After `Watching`, run `zeus-life flight list` again to verify the matching trip and its next read. To verify execution, look for a completed poll and an advancing next read. If the first poll is still pending past its nominal due time, check `hermes cron status` for the gateway heartbeat and recheck on the next scheduler tick or a quiet continuation. A due timestamp is not an execution timestamp; repeated list calls seconds apart don't exercise the watch.
3. Keep the native watch as the sole sender and the tracker. It polls the flights, and it sends the layover, delay, gate, leave-by and landed texts itself. Work-item turns that keep re-checking it add cost and send nothing. So once step 2 shows the trip watched with a completed poll, finish the `zeus_work` item: its criteria are the itinerary and the active watch, nothing after the trip. Do not add "reconcile the alerts after the trip" or any criterion that keeps the item open to watch the watcher. If the owner later asks whether a text went out, read `$HERMES_HOME/workspace/life-engine/ledger.sqlite3` with `sqlite3 -readonly` (`opps.key` = `flight:<trip-id>:*`, joined to `note_items` and `outbox`); a live `sent` row is the sender's record, not handset confirmation. Use a timed sending fallback only with clear ownership of the event, never as a parallel sender.

### Browser access: reuse the current flight tab

When browser access is needed for a flight check, use only an already-open flight tab. Refresh that same tab for a fresh read and do not create, close, or change any other tab. Do not use a browser helper or fallback that opens a tab automatically, even briefly. If the existing flight tab cannot be reused without touching other tabs, skip the browser step and continue with available non-browser sources. The tracker and cross-check guidance below is subject to this rule. Saving this procedure does not modify a live watch or its schedule.

### Resolve the itinerary first

If the owner points to an itinerary email, look in their verified mailbox for the current request's traveler and received-time window. If direct search misses, check the latest authorized inbox snapshot for the message body and received time. If connection labels or cached account IDs disagree with a candidate email, verify the actual mailbox profile on the same active connection that returned the message and match its delivery address before using it. Don't assume a connection nickname identifies the mailbox. Match the traveler in the body, not just the sender or subject, because one sender can supply different travelers' itineraries. Use older threads only as leads, never as a substitute for a matching current itinerary. Then match the flight number, departure date, and airport pair for each leg against a dated status page. If the owner instead gives only a description (route, date, stops, arrival time), search the public schedule for the match. Do not ask whether they want other options or a look inside a reservation. Do not open the owner's own airline account for someone else's flight. Their account only lists reservations in their name, so a missing card is not evidence the flight is gone.

Match stops and the stated arrival time against the public listing. Departure times are origin-local and arrival times are destination-local. Read AM/PM off the listing. A bare clock time is not morning. A unique match is the itinerary to track. It is not proof of the confirmation number, fare, or seat. Say that limit. Do not select a fare, book, or pay. If more than one itinerary still fits, name the candidates in one line and ask which one.

For a known flight number, open FlightStats at `https://www.flightstats.com/v2/flight-tracker/CARRIER/NUMBER?year=YYYY&month=M&date=D`. Verify the airport pair in the status block before using its status: a number can serve several legs on the same date. If the default block is the wrong leg, select the correct route from the dated flight list and recheck the status block. Keep the resulting `flightId` URL in the work record so subsequent checks open the same segment. If extracted text appears stale, open the exact dated segment in the real browser and follow its "View Flight Details" link. Compare the provider's event timestamps and read the latest gate and runway fields separately: a newer retrieval of the basic tracker can still show older data than the rendered Details timeline. Confirm the date, airport pair and read time. Actual wheels-up or touchdown is not actual gate-out or gate-in. If FlightStats stays on "Scheduled" past departure, cross-check FlightAware's live tracker using the carrier's ICAO designator and flight number; verify the airport pair and date there too. Stale flight status or a missing receipt does not establish that the watch hasn't sent or won't send. Before a manual fallback, check the event's outbound record and establish that the watch won't also send for that event. If the send state is uncertain, don't replay it. Southwest route discovery is in `references/southwest-schedule-lookup.md`.

### Timed fallback and pickup estimate

1. If using one-shots for requested layover texts, schedule one check per connection, just after expected gate arrival and before the onward flight. Check both legs: actual inbound arrival, onward status, and final ETA. If the inbound flight hasn't arrived, say so; if the revised connection still calls for a text, reconcile existing jobs before scheduling one replacement check for the actual layover. For a nonstop watch, use a post-departure check instead.
2. For pickup, check the gate ETA and traffic early enough for the owner to act. Target curbside at the gate ETA plus time to deplane and collect bags, then subtract the drive time and traffic/terminal allowance to get the leave-by time. If traffic is unavailable, label the drive estimate and buffer; don't call them live. State the assumed starting point if the owner didn't confirm it for this pickup. A cancellation means don't drive on that itinerary.

For one-shots, put the flight date, airport pairs, scheduled times, dated tracker links, pickup origin and terminal, and leave-time rule in the job prompts; the scheduled turn doesn't have this chat. Use `references/hermes-cron-chat-check.md` for scheduling. For either watch type, only message the owner and report confirmed status or uncertainty. When an inbound delay tightens the connection, call out the risk even if the onward flight is still on time. The gap to scheduled departure is not usable transfer time: account for deplaning, the terminal transfer and boarding cutoff where known. An aircraft's status does not establish that the traveler boarded. Lead with the provisional leave time if pickup is requested, then give the current flight status and the verified alert plan in the owner's local zone. For a native watch, describe the layover or pickup event, not the poll cadence. For one-shots, give the scheduled text times and describe them as timed checks, not continuous monitoring.

If the useful check falls in quiet hours, still schedule it when missing it means a missed flight or pickup, and say that in the reply. A completed one-shot for the same item is already stopped; do not add a cancel job.

## Pitfalls

- **Bulk-read Reminders properties** (`name of (reminders of L whose completed is false)`). Looping per reminder with property reads can take longer than 40s.
- **Use `revise` only after an explicit scope correction from the user.** Record routine progress with `checkpoint`, and put job ids in the checkpoint's next action.
- **Promise only what a fresh readback shows:** a scheduled job with an exact `next_run_at`. Saved is not scheduled.
- **Recurring daily jobs that call a model need retry slots.** A `--no-agent` script that shells out to a one-shot model run can time out or exit 1, and a once-a-day cron then means no delivery that day. Check `last_status` and `last_error` in jobs.json after the run time, and give the job a few close slots with `hermes cron edit <job_id> --schedule "0,20,40 9 * * *"` (HERMES_HOME set to the active profile). Retries only help if the script exits quietly when that day's output is already recorded, so confirm that before adding slots, otherwise a successful run gets duplicated. Read the new `expr` and `next_run_at` back from jobs.json. If you deliver a missed run's content by hand, record it in the job's own store so the next run does not repeat it.
- **Recurring digests to the owner carry context, not lists.** Each item in a scheduled brief needs its context, a recommendation and the next step. Don't schedule a bare relay-style list.
- **The inbox watcher is the exception:** never add cron or launchd for the agent inbox. Its own loop already covers it.
- **A completed one-shot is already stopped.** If the job state is completed or disabled, do not create another job to cancel it. Say it already ran. If a separate recurring scanner can resurface the same item, name that limit instead of promising silence.
- **"Pick a random minute between A and B, then post":** draw once with `jot -r 1 <lo> <hi>` and show the drawn time. Keep that value fixed for the job, and never redraw it on a later turn. Then schedule a one-time job for that minute, like any other timed post. If the target account or channel can't be reached, use the outbound-messages-and-files rules: show the drawn time and the drafted text, and ask one question.

## Reply style

- First line: scheduled (with the exact time) or not scheduled, plus the one missing thing.
- Say "automatic checks are off" rather than naming internal tools.
- One question at most, and make it the one that unblocks scheduling.
