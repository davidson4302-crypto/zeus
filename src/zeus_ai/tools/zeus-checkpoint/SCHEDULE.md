# zeus-checkpoint: suggested schedule

The tool does not schedule itself. These steps set it up as script jobs in Zeus's existing scheduler (Hermes cron,
profile `zeus`): no model, no new scheduler, no new watcher. None of them sends anything to anybody.

The flags below are the scheduler's own (`hermes cron create`: `--script`, `--no-agent`, `--deliver`,
`--failure-deliver`). A script job may run for up to one hour (the scheduler's default script timeout).

Paths below use these names:

- `CODE`: the folder that holds `zeus_checkpoint.py` and `drill_engine.py` (both files, side by side).
- `ENGINE`: the life engine folder (default: settings `checkpoint.engine_dir`, else the package's `engine/` folder).
- `PROFILE_DIR`: the Hermes profile folder (settings `hermes.profile_dir`, default `~/.hermes/profiles/zeus`).

## 1. The launcher: `~/.local/bin/zeus-checkpoint`

```sh
#!/bin/sh
# zeus-checkpoint: Zeus's work-and-history backups and the restore drill.
# Code: CODE/zeus_checkpoint.py   Store: settings checkpoint.store   Schedule: SCHEDULE.md beside the code
exec python3 -I -B CODE/zeus_checkpoint.py "$@"
```

Python 3.11 or newer.

## 2. Three scripts in `PROFILE_DIR/scripts/`

`zeus_checkpoint_small.sh`

```sh
#!/bin/sh
# Every 6 hours: save Zeus's work stores and small state.
exec "$HOME/.local/bin/zeus-checkpoint" take --small -m "every 6 hours"
```

`zeus_checkpoint_full.sh`

```sh
#!/bin/sh
# Once a day: also the chat history and the memory outbox, then remove checkpoints older than 30 days.
"$HOME/.local/bin/zeus-checkpoint" take --full -m "daily"; rc=$?
"$HOME/.local/bin/zeus-checkpoint" prune --keep-days 30 || rc=1
exit $rc
```

`zeus_checkpoint_drill.sh`

```sh
#!/bin/sh
# Monthly restore drill. The job fires every hour on the 1st and 2nd; the tool itself runs the drill once, at the
# first run at or after the drill hour (settings checkpoint.drill_hour, default 10) on the 1st where the owner is
# (owner-zone.json; the owner's home time zone when there is none), and says "No drill now" every other time.
# A month whose 1st was missed is caught up once at the next run.
exec "$HOME/.local/bin/zeus-checkpoint" drill --monthly --engine ENGINE
```

## 3. Three jobs

| name | schedule (`kind: cron`, scheduler clock) | script | no_agent | deliver | failure_deliver |
|---|---|---|---|---|---|
| `zeus-checkpoint small` | `0 */6 * * *` (midnight, 6am, noon, 6pm) | `zeus_checkpoint_small.sh` | true | `local` | `local` |
| `zeus-checkpoint full` | `30 3 * * *` (3:30am) | `zeus_checkpoint_full.sh` | true | `local` | `local` |
| `zeus-checkpoint drill` | `0 * 1,2 * *` (hourly on the 1st and 2nd; the tool's own gate picks the drill hour on the 1st, owner-local) | `zeus_checkpoint_drill.sh` | true | `local` | `local` |

`prompt` empty, `repeat.times` null (repeating), `workdir` null, no model, no skills.

Created with the scheduler's own command, once each:

```sh
hermes -p zeus cron create "0 */6 * * *" --name "zeus-checkpoint small" --script zeus_checkpoint_small.sh --no-agent --deliver local --failure-deliver local
hermes -p zeus cron create "30 3 * * *"  --name "zeus-checkpoint full"  --script zeus_checkpoint_full.sh  --no-agent --deliver local --failure-deliver local
hermes -p zeus cron create "0 * 1,2 * *" --name "zeus-checkpoint drill" --script zeus_checkpoint_drill.sh --no-agent --deliver local --failure-deliver local
```

Why these times: the jobs send nothing, so quiet hours are not in play. The full save runs at night so it is done
before the day starts. A small and a full save never run at the same moment (the tool also holds one lock, so if they
ever did, the second waits).

Why the drill job is hourly on two days: the scheduler's clock does not travel with the owner. When the owner is far
east, the drill hour on their 1st can still be the last day of the month on the scheduler's clock; the first run on
the scheduler's 1st then finds it is already past the drill hour on the owner's 1st and runs. When the owner is west,
their drill hour comes a few hours later. The tool decides, in code.

## 4. Before changing Zeus

```sh
zeus-checkpoint take --full -m "before <change>"
```

It prints the checkpoint's id. Exit code 0 means every store was saved and checked; 1 means the checkpoint was made
but something is missing from it (the reason is printed and is in its `manifest.json`): do not go ahead on a 1.

## 5. What to look at afterwards

- `zeus-checkpoint status`: last small, last full, total size, last drill, last prune.
- `<store>/status.json`: the same as a file (the scorecard reads it). `drill.ok`, `last_small.ended_at`, `total_bytes`.
- `<store>/drills/`: one record per drill.

## 6. Keeping a copy on another disk

Backups stay on this machine. To keep a second copy, point settings `checkpoint.store` at a folder on an external or
synced disk, or include the store folder in your own backup tool (it holds no credentials: see the tool's docstring).
To check a copy that came back from elsewhere:

```sh
zeus-checkpoint verify <id>          # every hash and every database copy
zeus-checkpoint drill <id> --engine ENGINE
```

Putting a checkpoint back over LIVE data is not a command in this tool.

## 7. Notes

- Kept for 30 days by default. The daily job ends with `zeus-checkpoint prune --keep-days 30`: whole checkpoints older
  than 30 days go; the newest small and the newest full always stay.
- A failed save is recorded, not announced: `--failure-deliver local` keeps failures out of the owner's messages.
  Nobody is told until something reads `status.json`.
