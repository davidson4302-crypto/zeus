"""Zeus work upkeep, run by Hermes cron every minute with no model call (`--no-agent`).

Loads the zeus-work plugin's native implementation directly (no CLI start-up) and runs one bounded
maintenance pass. It never wakes the agent, never sends and never calls a model; it prints a JSON status.
"""
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ZEUS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ZEUS / 'engine'))
import settings  # noqa: E402

home = Path(settings.PROFILE_DIR)
os.environ['HERMES_HOME'] = str(home)
os.environ['HERMES_PROFILE'] = home.name
os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
sys.path.insert(0, str(settings.HERMES_AGENT_DIR))
import hermes_bootstrap  # noqa: E402,F401
from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # noqa: E402

plugin = ZEUS / 'plugins' / 'zeus-work'
spec = importlib.util.spec_from_file_location('_zeus_native_maintenance', plugin / '__init__.py',
                                              submodule_search_locations=[str(plugin)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Native = importlib.import_module(spec.name + '.native').Native
selected = set_hermes_home_override(home)
try:
    status = Native(home).maintenance(limit=3)
finally:
    reset_hermes_home_override(selected)
assert status['wakeAgent'] is False
assert status['sender'] is False and status['llm'] is False
print(json.dumps(status))
