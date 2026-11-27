"""Purchase settle check producer for the purchase lock (Hermes cron monitor script).

Prints one CHECK line per order whose Place order result is not confirmed yet, only while the owner may be
contacted (outside quiet hours). Empty output = nothing to check, so no model turn starts.
"""
import importlib.machinery
import importlib.util
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ZEUS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ZEUS / 'engine'))
from ownertime import alert_open  # noqa: E402

BUY = ZEUS / 'bin' / 'zeus-buy'
spec = importlib.util.spec_from_file_location('zeusbuy', str(BUY), loader=importlib.machinery.SourceFileLoader('zeusbuy', str(BUY)))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
F = B.F


def contact_open(life):
    at = datetime.fromtimestamp(time.time(), timezone.utc)
    return alert_open(at, life=life) <= at


life = F.life_home()
if not contact_open(life):
    sys.exit(0)
try:
    st = F.Stores()
    data = F.Grants(life).load()
except Exception as error:
    print(f'The purchase grants could not be read ({type(error).__name__}). Tell the owner in one line that the purchase lock needs a look.')
    sys.exit(0)
lines = []
for g, n in B.due_checks(data, time.time()):
    lines.append(f'CHECK {n}: {st.name(g["store"])} order ({g.get("what")}, up to {F.dollars(int(g["total_cents"]))}), Place order '
                 f'pressed at {F.clock(g.get("pressed_at") or g.get("at"))}. Orders page: {st.orders_url(g["store"])} '
                 f'(store id {g["store"]}).')
if lines and contact_open(life):
    print('\n'.join(lines))
