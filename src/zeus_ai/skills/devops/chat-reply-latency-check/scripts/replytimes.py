#!/usr/bin/env python3
"""Read-only: reply timings for one Photon iMessage chat, from gateway logs.
Per inbound text: seconds until the reply was ready, model call count, and seconds from
ready to hand-off to Photon. Sends nothing.

Usage: python3 -I replytimes.py --sender '<masked number as logged>' [--since 'YYYY-MM-DD HH:MM']
The profile log is $HERMES_HOME/logs/gateway.log; the main log is <hermes home>/logs/gateway.log."""
import argparse
import os
import re
import statistics
from datetime import datetime
from pathlib import Path

ap = argparse.ArgumentParser(description="Reply timings for one Photon chat, from gateway logs (read-only).")
ap.add_argument("--sender", required=True, help="the chat's masked number exactly as the gateway log shows it")
ap.add_argument("--since", default=datetime.now().strftime("%Y-%m-%d 00:00"), help="start of the window, 'YYYY-MM-DD HH:MM' (default: today)")
ap.add_argument("--profile-log", type=Path, help="default: $HERMES_HOME/logs/gateway.log")
ap.add_argument("--main-log", type=Path, help="default: ~/.hermes/logs/gateway.log")
args = ap.parse_args()
H = Path.home() / ".hermes"
PROFILE_LOG = args.profile_log or Path(os.environ.get("HERMES_HOME") or H / "profiles/zeus") / "logs/gateway.log"
MAIN_LOG = args.main_log or H / "logs/gateway.log"
SINCE = args.since
SENDER_MASK = args.sender
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ")


def t(line):
    m = TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2)) / 1000 if m else None


inbound, ready = [], []
for line in PROFILE_LOG.read_text(errors="replace").splitlines():
    if line[:16] < SINCE or "platform=photon" not in line:
        continue
    if "inbound message:" in line:
        msg = re.search(r"msg='(.*)' reply_to_id", line)
        inbound.append((t(line), (msg.group(1) if msg else "")[:40]))
    elif "response ready:" in line:
        secs = re.search(r"time=([\d.]+)s", line)
        calls = re.search(r"api_calls=(\d+)", line)
        ready.append((t(line), float(secs.group(1)), int(calls.group(1))))
# Send lines land in the main log or the profile log depending on which gateway served the turn.
sends = sorted({t(l) for f in (MAIN_LOG, PROFILE_LOG) if f.is_file() for l in f.read_text(errors="replace").splitlines()
                if l[:16] >= SINCE and "[Photon] Sending response" in l and SENDER_MASK in l})

rows = []
for ts_in, text in inbound:
    r = next((x for x in ready if x[0] >= ts_in), None)
    if not r:
        continue
    s = next((x for x in sends if x >= r[0] - 0.05), None)
    rows.append((datetime.fromtimestamp(ts_in).strftime("%H:%M:%S"), text, r[1], r[2], (s - r[0]) if s else None))

for when, text, secs, calls, handoff in rows:
    print(f"{when}  {secs:6.1f}s  calls={calls}  handoff={handoff if handoff is None else round(handoff, 2)}s  {text}")
simple = [r[2] for r in rows if r[3] == 1]
tools = [r[2] for r in rows if r[3] > 1]
hand = [r[4] for r in rows if r[4] is not None]
if simple:
    print(f"simple replies: n={len(simple)} median={statistics.median(simple):.1f}s min={min(simple):.1f}s max={max(simple):.1f}s")
if tools:
    print(f"replies with lookups: n={len(tools)} median={statistics.median(tools):.1f}s min={min(tools):.1f}s max={max(tools):.1f}s")
if hand:
    print(f"ready -> handed to Photon: median={statistics.median(hand):.2f}s max={max(hand):.2f}s")
