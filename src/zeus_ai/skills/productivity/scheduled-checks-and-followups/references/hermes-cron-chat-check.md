# One-time check that posts its result to chat (Hermes cron)

## Script (deterministic, no LLM)

Put it under `$HERMES_HOME/scripts/` (the active profile's scripts folder). `--script` takes the bare filename. `.sh` files run with bash.

```bash
#!/bin/bash
F="/absolute/path/to/target.md"
NOW="$(date '+%-I:%M:%S %p %Z, %b %-d')"
if [ -f "$F" ]; then
  SIZE="$(stat -f %z "$F")"; MOD="$(stat -f '%Sm' -t '%-I:%M:%S %p %Z' "$F")"
  echo "11:59 check: target.md is there. Checked ${NOW}. ${SIZE} bytes, last changed ${MOD}."
else
  echo "11:59 check: target.md is still not there. Checked ${NOW}. Path: <short path> (not found)."
fi
```

Run it once by hand (`bash scripts/<name>.sh`) to make sure it prints one line. That run is also your baseline check. (`stat -f` is the macOS syntax.)

## Create the job

```bash
export HERMES_HOME="$HOME/.hermes/profiles/<profile>"
hermes cron create --name "<class-level-name>" \
  --deliver "slack:<chat_id>:<thread_ts>" \
  --repeat 1 --script <name>.sh --no-agent \
  "<min> <hour> <day> <month> *"
```

- Get `chat_id` and `thread_ts` from the gateway message origin (`chat_id` and `thread_id`). With `slack:<chat_id>:<thread_ts>`, the post lands in the same thread. If the job's origin matches, `slack:<chat_id>` alone also inherits the origin thread.
- A cron expression with a fixed day and month plus `--repeat 1` gives a single run. The schedule runs in host local time, so check that `Next run` shows the expected offset (for example `-07:00` for US Pacific daylight time).
- `deliver=local` saves output only. Never use it when the user asked for a post.

## Live checks with a text result

Use a model-based one-shot for a fresh lookup and an interpreted result:

```bash
hermes cron create --name '<check name>' --repeat 1 \
  --deliver '<verified delivery target>' '<ISO date/time with offset>' \
  '<self-contained lookup instructions and one short text for the owner>'
```

Use a platform delivery such as `--deliver photon` only after confirming that the active profile's home channel on that platform is the owner's private destination; otherwise use the verified explicit private route. The job should return the text for scheduled delivery, not send a second copy itself. Check for a newer stop or correction in the existing work record before producing the update.

## Readback

- `hermes cron list` shows the job id, `Next run`, and `Deliver`.
- The source of truth is `$HERMES_HOME/cron/jobs.json`: check `enabled: true`, `state: scheduled`, `next_run_at`, `deliver`, and `repeat.times: 1`.
- `hermes cron status` confirms the gateway ticker is alive. `hermes cron doctor` should report no issues.
- After the run: check `last_run_at`, `last_status`, `last_delivery_error`, and whether the post showed up in the thread.
