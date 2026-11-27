# Apple Reminders (the owner's iPhone-synced list)

The Mac's Reminders app syncs to the owner's iPhone through iCloud, so a reminder created here can ping their phone. Use it for reminders they ask for, once they have confirmed that a test reminder reached their phone (until then, the cron-to-chat recipe is the proven path; offer Apple Reminders as an addition, not a replacement).

## Read open reminders
Large `repeat ... whose` loops over each reminder time out. Fetch the names and due dates as two bulk lists, then zip them by index:

```
perl -e 'alarm 120; exec @ARGV' osascript -e 'tell application "Reminders"
set L to list "Reminders"
set ns to name of (reminders of L whose completed is false)
set ds to due date of (reminders of L whose completed is false)
set out to ""
repeat with i from 1 to count of ns
set d to item i of ds
if d is missing value then
set dstr to "no due date"
else
set dstr to d as string
end if
set out to out & item i of ns & " | " & dstr & linefeed
end repeat
return out
end tell'
```
The per-item version (`repeat with r in reminders ... due date of r`) hit the 40s alarm; the bulk version finished.

## Create one
Build the date with component setters (string literals are locale-dependent). Times are in the owner's timezone, which should be the Mac's local zone.

```
perl -e 'alarm 60; exec @ARGV' osascript -e 'set d to current date
set year of d to 2027
set month of d to March
set day of d to 12
set time of d to (9 * hours)
tell application "Reminders"
set r to make new reminder at end of list "Reminders" with properties {name:"<text>", due date:d, remind me date:d}
return id of r
end tell'
```
Set both `due date` and `remind me date`; the second one is what fires the alert.

## Verify
One run creates one reminder, so never re-run the create command if the output is slow or odd. Read it back by name and check count, due date, alert date and `completed` (`reminders of list "Reminders" whose name is "<text>"`). The group-container sqlite store is not readable from the agent, so don't try it. The readback proves the Mac side only; say the phone arrival is unconfirmed until the owner says it went off.

## Guardrails
Ticking off or deleting reminders counts as deleting, so ask the owner first. Creating is fine.
