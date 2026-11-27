#!/bin/sh
# Zeus backups, run by Hermes cron. The mode comes from the name it is called by:
#   zeus_backup_small.sh  every 6 hours, work stores and small state
#   zeus_backup_full.sh   nightly, also chat history
#   zeus_backup_drill.sh  restore drill on a throwaway copy
# Sends nothing to anybody.
SELF="$0"
while [ -L "$SELF" ]; do
  LINK=$(readlink "$SELF"); case "$LINK" in /*) SELF="$LINK";; *) SELF="$(dirname "$SELF")/$LINK";; esac
done
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
case "$(basename "$0")" in
  *small*) exec "$ROOT/bin/zeus-checkpoint" take --small -m "every 6 hours" ;;
  *full*)  exec "$ROOT/bin/zeus-checkpoint" take --full -m "nightly" ;;
  *drill*) exec "$ROOT/bin/zeus-checkpoint" drill ;;
  *) echo "call this as zeus_backup_small.sh, zeus_backup_full.sh or zeus_backup_drill.sh" >&2; exit 2 ;;
esac
