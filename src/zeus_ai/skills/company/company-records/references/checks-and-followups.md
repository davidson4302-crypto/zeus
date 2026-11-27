# Promised checks and file-existence checks

## Before promising a later check

The full procedure is in the scheduled-checks-and-followups skill, including the chat-posting cron recipe. In short:

1. Find out where the result should land. By default it's a post in the chat where the request was made.
2. Promise a check only from a fresh readback: a scheduled job with an exact next_run_at. Quote the time with its zone.
3. If scheduling is blocked, name the exact missing setting, quote the refusal, and ask one unblocking question. Never offer "message me at that time and I'll check".
4. A request for an automatic check is itself the yes to schedule one. Name what the job will run and where it will post.

## Reading the clock

Run date with the zone before you say how far away a time is or whether "today" has passed. State the zone in the reply, for example "today at 11:59 AM Pacific".

## File-existence checks

1. Find the base of a relative path. Test each candidate absolute path with a plain existence test: the agent's working directory first, then the profile directory.
2. If no candidate exists, search for the exact filename under the Hermes tree (search_files, target files). A result with truncated or total_count_is_lower_bound set does not prove absence. Narrow the path or pattern and search again.
3. Report the paths tested, the search scope, the time read, and the result. If the file could live outside those folders, ask for the full path.
4. Never substitute a similar-looking file. It is a different file.
5. A check reports the state at the moment it runs. For a later time, run it again then.
