"""Zeus settings: the one place that knows who the owner is and where things live.

Every module reads owner details, paths and service choices from here, never from a hardcoded value.
Values come from (highest first): environment variables, the config file, the defaults below.

Config file: $ZEUS_CONFIG, else ~/.zeus/config.toml (written by `zeus setup`). Example:

    [owner]
    name = "Alex Rivera"            # full name, used in prompts ("Alex")
    first_name = "Alex"
    phone = "+15555550100"          # the owner's iMessage handle
    timezone = "America/New_York"

    [owner.emails]                  # label = address; every mailbox Zeus reads for the owner
    personal = "alex@example.test"

    [agent]
    name = "Zeus"
    email = "zeus@example.test"     # the agent's own AgentMail inbox (optional)

    [quiet_hours]
    start = 21                      # 9pm, owner's clock
    end = 9                         # 9am

    [calendar]
    ics_urls = []                   # private iCal feed URLs, read-only

Standard library only (Python 3.11+). Importing this module never fails: a missing or broken config file
gives the defaults, and `zeus doctor` reports what is missing.
"""
from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = ENGINE_DIR.parent
HOME = Path.home()

CONFIG_FILE = Path(os.environ.get('ZEUS_CONFIG') or HOME / '.zeus' / 'config.toml')


def _load() -> dict:
    try:
        with open(CONFIG_FILE, 'rb') as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


_CONFIG = _load()


def get(dotted: str, default=None):
    """Read any config value by dotted name, e.g. get('calendar.ics_urls', []). Env ZEUS_<DOTTED_UPPER> wins."""
    env = os.environ.get('ZEUS_' + dotted.upper().replace('.', '_'))
    if env is not None:
        return env
    node = _CONFIG
    for part in dotted.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def reload() -> None:
    global _CONFIG
    _CONFIG = _load()


# ---- who --------------------------------------------------------------------------------------------
OWNER_NAME: str = get('owner.name', 'the owner')
OWNER_FIRST: str = get('owner.first_name', OWNER_NAME.split()[0] if OWNER_NAME != 'the owner' else 'the owner')
OWNER_PHONE: str = get('owner.phone', '')
OWNER_TZ: str = get('owner.timezone', 'America/Los_Angeles')
OWNER_EMAILS: dict[str, str] = dict(get('owner.emails', {}) or {})
OWN_ADDRESSES: frozenset[str] = frozenset(a.lower() for a in OWNER_EMAILS.values())
OWN_DOMAINS: frozenset[str] = frozenset(get('owner.domains', []) or [])

AGENT_NAME: str = get('agent.name', 'Zeus')
AGENT_EMAIL: str = get('agent.email', '')

QUIET_START: int = int(get('quiet_hours.start', 21))
QUIET_END: int = int(get('quiet_hours.end', 9))

# Other agents or people the owner treats as internal team (never approvers). Empty by default.
TEAM: list[dict] = list(get('team.members', []) or [])

# ---- where ------------------------------------------------------------------------------------------
PROFILE: str = get('hermes.profile', 'zeus')
HERMES_HOME = Path(get('hermes.home', str(HOME / '.hermes')))
PROFILE_DIR = Path(get('hermes.profile_dir', str(HERMES_HOME / 'profiles' / PROFILE)))
HERMES_AGENT_DIR = Path(get('hermes.agent_dir', str(HERMES_HOME / 'hermes-agent')))
LIFE_HOME = Path(os.environ.get('LIFE_HOME') or get('paths.life_home', str(PROFILE_DIR / 'workspace' / 'life-engine')))
STATE_DB = Path(get('paths.state_db', str(PROFILE_DIR / 'state.db')))
MEMORY_FILE = Path(get('paths.memory_file', str(PROFILE_DIR / 'memories' / 'MEMORY.md')))
CACHE_DIR = Path(get('paths.cache_dir', str(PROFILE_DIR / 'cache')))
CHROME_DATA_DIR = Path(get('paths.chrome_data_dir', str(HOME / '.zeus' / 'chrome')))


def _bin(name: str, key: str) -> str:
    return get('bin.' + key) or shutil.which(name) or str(HOME / '.local' / 'bin' / name)


HERMES_BIN = _bin('hermes', 'hermes')
CLAUDE_BIN = _bin('claude', 'claude')
COMPOSIO_BIN = _bin('composio', 'composio')
GOG_BIN = _bin('gog', 'gog')


def owner_label() -> str:
    """How prompts name the owner: first name if set, else 'the owner'."""
    return OWNER_FIRST
