-- Read-only local Calendar adapter. Output is a local cache observation, not server freshness proof.
use framework "Foundation"
use scripting additions
-- Edit START_*, DAY_COUNT and EXPECTED_ZONE (the owner's IANA timezone), then run: perl -e 'alarm 120; exec @ARGV' osascript this_file.applescript
set START_YEAR to 2027
set START_MONTH to March
set START_DAY to 1
set DAY_COUNT to 7
set EXPECTED_ZONE to "America/Los_Angeles"

set localZone to current application's NSTimeZone's localTimeZone()
set zoneName to (localZone's |name|()) as text
if zoneName is not EXPECTED_ZONE then error "Calendar coverage unavailable: Mac timezone must be " & EXPECTED_ZONE & " for this adapter."
set failedReads to 0
set recurringSeries to 0
set calendarCount to 0
set identityFailures to 0
set unverifiedIdentityRows to 0
set startD to current date
set day of startD to 1
set year of startD to START_YEAR
set month of startD to START_MONTH
set day of startD to START_DAY
set time of startD to 0
set endD to startD + (DAY_COUNT * days)
set out to "== WINDOW start=" & my isoDate(startD) & "; end_exclusive=" & my isoDate(endD) & "; timezone=" & zoneName & " ==" & linefeed
set out to out & "== EVENTS IN RANGE ==" & linefeed
tell application "Calendar"
	repeat with c in calendars
		set cn to my cleanField(name of c)
        set calendarCount to calendarCount + 1
        set identityInfo to my calendarIdentity(c)
        set calendarKey to item 1 of identityInfo
        set calendarIdentityVerified to item 2 of identityInfo
        if not calendarIdentityVerified then set identityFailures to identityFailures + 1
        try
			set evs to (every event of c whose start date < endD and end date > startD)
            repeat with e in evs
                set eventIdentityInfo to my eventIdentity(e, calendarIdentityVerified)
                set eventKey to item 1 of eventIdentityInfo
                set rowIdentity to item 2 of eventIdentityInfo
                if rowIdentity is "unverified" then set unverifiedIdentityRows to unverifiedIdentityRows + 1
                if item 3 of eventIdentityInfo then set identityFailures to identityFailures + 1
				set out to out & cn & " | " & (my cleanField(summary of e)) & " | " & ((start date of e) as string) & " | " & ((end date of e) as string) & " | allday=" & (allday event of e) & " | calendar_id=" & calendarKey & " | event_uid=" & eventKey & " | identity=" & rowIdentity & linefeed
			end repeat
		on error errMsg
			set failedReads to failedReads + 1
			set out to out & cn & " | ERROR " & my cleanField(errMsg) & linefeed
		end try
	end repeat
	set out to out & "== RECURRING SERIES (check if any fall in range) ==" & linefeed
	repeat with c in calendars
		set cn to my cleanField(name of c)
        set identityInfo to my calendarIdentity(c)
        set calendarKey to item 1 of identityInfo
        set calendarIdentityVerified to item 2 of identityInfo
        if not calendarIdentityVerified then set identityFailures to identityFailures + 1
        try
			set evs to (every event of c whose recurrence is not "")
            repeat with e in evs
                set eventIdentityInfo to my eventIdentity(e, calendarIdentityVerified)
                set eventKey to item 1 of eventIdentityInfo
                set rowIdentity to item 2 of eventIdentityInfo
                if rowIdentity is "unverified" then set unverifiedIdentityRows to unverifiedIdentityRows + 1
                if item 3 of eventIdentityInfo then set identityFailures to identityFailures + 1
				set recurringSeries to recurringSeries + 1
				set out to out & cn & " | " & (my cleanField(summary of e)) & " | " & ((start date of e) as string) & " | " & my cleanField(recurrence of e) & " | calendar_id=" & calendarKey & " | event_uid=" & eventKey & " | identity=" & rowIdentity & linefeed
			end repeat
		on error errMsg
			set failedReads to failedReads + 1
			set out to out & cn & " | ERROR " & my cleanField(errMsg) & linefeed
		end try
	end repeat
end tell
set out to out & "== READ COMPLETE calendars=" & calendarCount & "; failed_reads=" & failedReads & "; unexpanded_recurring_series=" & recurringSeries & "; identity_failures=" & identityFailures & "; identity_unverified_rows=" & unverifiedIdentityRows & "; freshness=unverified_local_cache ==" & linefeed
return out

on isoDate(d)
 return (year of d as text) & "-" & text -2 thru -1 of ("0" & ((month of d) as integer)) & "-" & text -2 thru -1 of ("0" & (day of d))
end isoDate

on cleanField(value)
 set t to value as text
 repeat with pair in {{"\\", "\\\\"}, {"|", "\\p"}, {return, "\\r"}, {linefeed, "\\n"}}
  set oldDelimiters to AppleScript's text item delimiters
  set AppleScript's text item delimiters to item 1 of pair
  set parts to text items of t
  set AppleScript's text item delimiters to item 2 of pair
  set t to parts as text
  set AppleScript's text item delimiters to oldDelimiters
 end repeat
 return t
end cleanField

-- Optional stable IDs must not make otherwise readable source rows disappear.
-- Empty fields mean unknown identity. Names and invented IDs are never substitutes.
on calendarIdentity(c)
 try
  tell application "Calendar" to set keyValue to calendarIdentifier of c
  set keyText to my cleanField(keyValue)
  if keyText is "" then return {"", false}
  return {keyText, true}
 on error
  return {"", false}
 end try
end calendarIdentity

on eventIdentity(e, calendarIdentityVerified)
 -- A failed calendar-ID property already makes the row unverified. Avoid
 -- hundreds of optional ID requests against that known-unavailable ID route.
 if not calendarIdentityVerified then return {"", "unverified", false}
 try
  tell application "Calendar" to set keyValue to uid of e
  set keyText to my cleanField(keyValue)
  if keyText is "" then return {"", "unverified", true}
  return {keyText, "verified", false}
 on error
  return {"", "unverified", true}
 end try
end eventIdentity
