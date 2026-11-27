"""`zeus setup`: install Zeus into a Hermes profile, step by step, safe to run again.

Order: prerequisites (Hermes, Photon, Composio) -> owner details -> Hermes profile -> config and character ->
plugins, skills and job scripts -> Hermes safety patches -> Photon iMessage line -> Composio Gmail ->
keys -> scheduled jobs -> background services -> gateway.

Every step checks first and changes only what is missing. `--dry-run` prints what it would do and changes nothing.
Nothing here sends a message, buys anything or deletes user data.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

PKG = Path(__file__).resolve().parent
TEMPLATES = PKG / 'templates'
PLUGINS = ['aaa-zeus-scope', 'agentmail', 'cross-surface-recall', 'imessage-approvals', 'life-items', 'life-work-guard',
           'outbound-message-context', 'own-agentmail', 'purchase-lock', 'quiet-personal-replies', 'zeus-guidance',
           'zeus-insight', 'zeus-work']
JOB_SCRIPTS = {'zeus_maintenance_tick.py': 'jobs/maintenance_tick.py', 'zeus_buy_settle.py': 'jobs/buy_settle_monitor.py',
               'zeus_signin_links.py': 'watch/zeus_signin_links.py', 'zeus_backup_small.sh': 'jobs/checkpoint.sh',
               'zeus_backup_full.sh': 'jobs/checkpoint.sh', 'zeus_backup_drill.sh': 'jobs/checkpoint.sh'}
def hermes_pin() -> str:
    """The public Hermes commit Zeus's patches are built and tested on (hermes_patches/series.json)."""
    try:
        return str(json.loads((PKG / 'hermes_patches' / 'series.json').read_text())['base_commit'])
    except (OSError, KeyError, ValueError):
        return ''


HERMES_INSTALL = ('curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --commit ' + hermes_pin()).rstrip()
COMPOSIO_INSTALL = 'curl -fsSL https://composio.dev/install | sh'
LINKS = {'hermes': 'https://github.com/NousResearch/hermes-agent', 'photon': 'https://photon.codes',
         'composio': 'https://composio.dev'}
E164 = re.compile(r'^\+[1-9]\d{7,14}$')


@dataclass
class Ctx:
    home: Path = field(default_factory=Path.home)
    profile: str = 'zeus'
    dry_run: bool = False
    yes: bool = False                    # accept defaults, never prompt (CI and tests)
    answers: dict = field(default_factory=dict)
    done: list = field(default_factory=list)

    @property
    def zeus_dir(self) -> Path:
        return self.home / '.zeus'

    @property
    def config_file(self) -> Path:
        return Path(os.environ.get('ZEUS_CONFIG') or self.zeus_dir / 'config.toml')

    @property
    def hermes_home(self) -> Path:
        return Path(os.environ.get('HERMES_HOME_ROOT') or self.home / '.hermes')

    @property
    def profile_dir(self) -> Path:
        return self.hermes_home / 'profiles' / self.profile

    @property
    def hermes_agent_dir(self) -> Path:
        return self.hermes_home / 'hermes-agent'

    def hermes(self) -> str | None:
        cand = shutil.which('hermes') or str(self.home / '.local' / 'bin' / 'hermes')
        return cand if Path(cand).exists() else None

    def hermes_python(self) -> str:
        for p in (self.hermes_agent_dir / 'venv' / 'bin' / 'python', self.hermes_agent_dir / '.venv' / 'bin' / 'python'):
            if p.exists():
                return str(p)
        return sys.executable


# ---- small helpers --------------------------------------------------------------------------------------
def say(msg: str) -> None:
    print(msg, flush=True)


def ask(ctx: Ctx, key: str, prompt: str, default: str = '', secret: bool = False, check=None) -> str:
    if key in ctx.answers:
        return str(ctx.answers[key])
    if ctx.yes:
        return default
    while True:
        shown = f' [{default}]' if default and not secret else ''
        raw = (getpass.getpass(f'{prompt}: ') if secret else input(f'{prompt}{shown}: ')).strip() or default
        if check is None or check(raw):
            return raw
        say('  That does not look right, try again.')


def confirm(ctx: Ctx, prompt: str, default: bool = True) -> bool:
    if ctx.yes:
        return default
    raw = input(f'{prompt} [{"Y/n" if default else "y/N"}]: ').strip().lower()
    return default if not raw else raw.startswith('y')


def run(ctx: Ctx, argv: list[str], *, check: bool = True, capture: bool = False, shell: bool = False, env=None):
    shown = argv if isinstance(argv, str) else ' '.join(shlex.quote(a) for a in argv)
    if ctx.dry_run:
        say(f'  would run: {shown}')
        return subprocess.CompletedProcess(argv, 0, '', '')
    r = subprocess.run(argv, text=True, capture_output=capture, shell=shell, env=env)
    if check and r.returncode:
        raise SystemExit(f'Failed: {shown}\n{(r.stderr or "").strip()}')
    return r


def write(ctx: Ctx, path: Path, text: str, mode: int | None = None) -> None:
    if ctx.dry_run:
        say(f'  would write {path}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def link(ctx: Ctx, target: Path, at: Path) -> None:
    if at.is_symlink() and Path(os.readlink(at)) == target:
        return
    if at.exists() and not at.is_symlink():
        say(f'  keeping your own {at} (not a Zeus link)')
        return
    if ctx.dry_run:
        say(f'  would link {at} -> {target}')
        return
    at.parent.mkdir(parents=True, exist_ok=True)
    if at.is_symlink():
        at.unlink()
    at.symlink_to(target)


def toml_dump(d: dict, prefix: str = '') -> str:
    """A small TOML writer for the config shape Zeus uses (tables, strings, numbers, bools, lists of scalars)."""
    def val(v):
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return '[' + ', '.join(val(x) for x in v) + ']'
        return json.dumps(str(v))
    out, tables = [], []
    for k, v in d.items():
        (tables if isinstance(v, dict) else out).append((k, v))
    lines = [f'{k} = {val(v)}' for k, v in out]
    for k, v in tables:
        name = f'{prefix}{k}'
        scalars = {a: b for a, b in v.items() if not isinstance(b, dict)}
        lines += ['', f'[{name}]'] + [f'{a} = {val(b)}' for a, b in scalars.items()]
        sub = {a: b for a, b in v.items() if isinstance(b, dict)}
        if sub:
            lines.append(toml_dump(sub, name + '.'))
    return '\n'.join(lines).strip('\n') + '\n'


def load_config(ctx: Ctx) -> dict:
    try:
        return tomllib.loads(ctx.config_file.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def env_set(ctx: Ctx, key: str, value: str) -> None:
    """Set KEY=value in the profile's .env (kept private, 0600)."""
    path = ctx.profile_dir / '.env'
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    lines = [l for l in lines if not l.startswith(key + '=')] + [f'{key}={value}']
    write(ctx, path, '\n'.join(lines) + '\n', mode=0o600)


def env_has(ctx: Ctx, key: str) -> bool:
    path = ctx.profile_dir / '.env'
    return path.exists() and any(l.startswith(key + '=') and l.split('=', 1)[1].strip()
                                 for l in path.read_text(encoding='utf-8').splitlines())


# ---- prerequisites ---------------------------------------------------------------------------------------
def prereq_status(ctx: Ctx) -> dict:
    composio = shutil.which('composio') or str(ctx.home / '.local' / 'bin' / 'composio')
    return {
        'macos': sys.platform == 'darwin',
        'hermes': ctx.hermes(),
        'hermes_source': ctx.hermes_agent_dir.is_dir(),
        'composio': composio if Path(composio).exists() else None,
        'node': shutil.which('node'),
        'npm': shutil.which('npm'),
        'chrome': next((p for p in ('/Applications/Google Chrome.app',
                                    str(ctx.home / 'Applications' / 'Google Chrome.app')) if Path(p).exists()), None),
    }


def step_prereqs(ctx: Ctx) -> None:
    say('\n1. Prerequisites: Hermes Agent, Photon, Composio')
    st = prereq_status(ctx)
    if not st['macos']:
        raise SystemExit('Zeus 1.0 runs on macOS only (iMessage, Reminders, Chrome).')
    if not st['hermes']:
        say(f'  Hermes Agent is not installed ({LINKS["hermes"]}).')
        if confirm(ctx, f'  Install it now with: {HERMES_INSTALL} ?'):
            run(ctx, HERMES_INSTALL, shell=True)
        else:
            raise SystemExit('Hermes Agent is required. Install it, run `hermes` once, then run `zeus setup` again.')
    if not st['composio']:
        say(f'  Composio CLI is not installed ({LINKS["composio"]}). Zeus reads your Gmail through it, read-only.')
        if confirm(ctx, f'  Install it now with: {COMPOSIO_INSTALL} ?'):
            run(ctx, COMPOSIO_INSTALL, shell=True)
        else:
            raise SystemExit('Composio is required. Install it, then run `zeus setup` again.')
    if not (st['node'] and st['npm']):
        if ctx.dry_run:
            say('  would stop here: Node.js and npm are missing (needed by Photon\'s iMessage bridge)')
        else:
            raise SystemExit('Photon\'s iMessage bridge needs Node.js and npm. Install Node.js (for example `brew install node`), then run `zeus setup` again.')
    if not st['chrome']:
        say('  Note: Google Chrome was not found. Store, flight and sign-in checks need it; everything else works.')
    say(f'  Photon ({LINKS["photon"]}) is set up in step 6 through Hermes (`hermes photon setup`).')
    ctx.done.append('prerequisites')


# ---- owner details -> ~/.zeus/config.toml -----------------------------------------------------------
def step_owner(ctx: Ctx) -> dict:
    say('\n2. About you (saved in ~/.zeus/config.toml, which never leaves this Mac)')
    cfg = load_config(ctx)
    owner = cfg.setdefault('owner', {})
    owner['name'] = ask(ctx, 'name', '  Your name', owner.get('name', ''))
    owner['first_name'] = ask(ctx, 'first_name', '  What Zeus should call you', owner.get('first_name') or owner['name'].split(' ')[0])
    owner['phone'] = ask(ctx, 'phone', '  Your iPhone number, with country code (like +15555550100)', owner.get('phone', ''),
                         check=lambda v: bool(E164.match(v)))
    owner['timezone'] = ask(ctx, 'timezone', '  Your time zone', owner.get('timezone') or _local_tz())
    emails = owner.setdefault('emails', {})
    primary = ask(ctx, 'email', '  Your main email address (Gmail)', emails.get('personal', ''))
    if primary:
        emails['personal'] = primary
    agent = cfg.setdefault('agent', {})
    agent.setdefault('name', 'Zeus')
    quiet = cfg.setdefault('quiet_hours', {})
    quiet['start'] = int(ask(ctx, 'quiet_start', '  Quiet hours start (hour, 0-23)', str(quiet.get('start', 21)), check=_hour))
    quiet['end'] = int(ask(ctx, 'quiet_end', '  Quiet hours end (hour, 0-23)', str(quiet.get('end', 9)), check=_hour))
    hermes = cfg.setdefault('hermes', {})
    hermes['profile'] = ctx.profile
    hermes['home'] = str(ctx.hermes_home)
    cfg.setdefault('engine', {})['python'] = ctx.hermes_python()
    write(ctx, ctx.config_file, '# Zeus settings. Edit freely; run `zeus doctor` after changes.\n' + toml_dump(cfg), mode=0o600)
    ctx.done.append('owner details')
    return cfg


def _hour(v: str) -> bool:
    return v.isdigit() and 0 <= int(v) <= 23


def _local_tz() -> str:
    try:
        return os.path.realpath('/etc/localtime').split('zoneinfo/', 1)[1]
    except (IndexError, OSError):
        return 'America/Los_Angeles'


# ---- Hermes profile, config, character -------------------------------------------------------------
def step_profile(ctx: Ctx, cfg: dict) -> None:
    say(f'\n3. Hermes profile "{ctx.profile}"')
    if not ctx.profile_dir.exists():
        run(ctx, [ctx.hermes() or 'hermes', 'profile', 'create', ctx.profile, '--description', 'Zeus personal assistant'])
    values = {
        'owner_tz': cfg['owner']['timezone'], 'quiet_start': cfg['quiet_hours']['start'], 'profile_dir': str(ctx.profile_dir),
        'skills_dir': str(PKG / 'skills'), 'chrome_port': cfg.get('chrome', {}).get('port', 9333),
        'chat_model': cfg.get('models', {}).get('main', 'claude-opus-5-5'),
        'work_model': cfg.get('models', {}).get('work', 'claude-sonnet-5-5'),
        'model_provider': cfg.get('models', {}).get('provider', 'anthropic'),
    }
    conf = ctx.profile_dir / 'config.yaml'
    marker = ctx.profile_dir / '.zeus-config'
    if not marker.exists() or ctx.answers.get('reset_config'):
        if conf.exists() and not ctx.dry_run:
            shutil.copy2(conf, conf.with_name('config.yaml.before-zeus'))
        write(ctx, conf, Template((TEMPLATES / 'profile-config.yaml').read_text()).substitute(values))
        write(ctx, marker, 'config.yaml was written by zeus setup\n')
    else:
        say('  config.yaml already set up by Zeus: kept (use --reset-config to rewrite it)')
    soul = ctx.profile_dir / 'SOUL.md'
    if not soul.exists() or ctx.answers.get('reset_config') or not (ctx.profile_dir / '.zeus-soul').exists():
        text = (TEMPLATES / 'SOUL.md').read_text().format(agent_name=cfg['agent']['name'], owner_first=cfg['owner']['first_name'])
        if soul.exists() and not ctx.dry_run:
            shutil.copy2(soul, soul.with_name('SOUL.md.before-zeus'))
        write(ctx, soul, text)
        write(ctx, ctx.profile_dir / '.zeus-soul', 'SOUL.md was written by zeus setup\n')
    ctx.done.append('profile')


def step_links(ctx: Ctx) -> None:
    say('\n4. Plugins, skills and job scripts')
    for name in PLUGINS:
        link(ctx, PKG / 'plugins' / name, ctx.profile_dir / 'plugins' / name)
    for skill in sorted((PKG / 'skills').glob('*/*/SKILL.md')):
        cat, name = skill.parent.parent.name, skill.parent.name
        link(ctx, skill.parent, ctx.profile_dir / 'skills' / cat / name)
    for at, rel in JOB_SCRIPTS.items():
        link(ctx, PKG / rel, ctx.profile_dir / 'scripts' / at)
    bindir = ctx.home / '.local' / 'bin'
    for tool in ('zeus-buy', 'zeus-life', 'zeus-login', 'zeus-checkpoint', 'zeus-snapshot'):
        link(ctx, PKG / 'bin' / tool, bindir / tool)
    for p in (PKG / 'bin').iterdir():
        if not ctx.dry_run and p.is_file():
            p.chmod(p.stat().st_mode | 0o111)
    (ctx.profile_dir / 'workspace' / 'life-engine').mkdir(parents=True, exist_ok=True) if not ctx.dry_run else None
    ctx.done.append('plugins and skills')


def step_patches(ctx: Ctx) -> None:
    say('\n5. Hermes safety patches (quiet hours hold, delivery fixes, iMessage reactions)')
    try:
        from . import hermes_patch
    except ImportError:
        say('  Patch module missing from this build: skipped. Quiet hours for scheduled sends will not hold.')
        return
    st = hermes_patch.status(ctx.hermes_agent_dir)
    conflicts = [p.get('file') for p in st.get('patches', []) if p.get('state') == 'conflict']
    if not st.get('ok') or conflicts:
        raise SystemExit(
            'Zeus\'s safety patches do not fit this Hermes install, so nothing was changed.\n'
            f'Zeus is tested on Hermes commit {hermes_pin()}. Options:\n'
            f'  - reinstall Hermes at that version: {HERMES_INSTALL}\n'
            '  - or wait for a Zeus release that supports your Hermes version.\n'
            'Details: `zeus hermes-patch status`.')
    if st.get('all_applied'):
        say('  already applied')
    elif ctx.dry_run:
        todo = [p.get('name') or p.get('file') for p in st.get('patches', []) if p.get('state') != 'applied']
        say(f'  would apply: {", ".join(map(str, todo)) or "nothing"}')
    else:
        res = hermes_patch.apply(ctx.hermes_agent_dir)
        if not res.get('ok'):
            raise SystemExit('Hermes patches did not apply: ' + str(res.get('error') or res) +
                             '\nRun `zeus hermes-patch status` for details. Your Hermes was not changed.')
        say('  applied')
    ctx.done.append('hermes patches')


def step_photon(ctx: Ctx, cfg: dict) -> None:
    say('\n6. Photon iMessage line (opens your browser to sign in to Photon)')
    if env_has(ctx, 'PHOTON_PROJECT_ID') and env_has(ctx, 'PHOTON_PROJECT_SECRET'):
        say('  already set up')
    else:
        say('  Heads up: each Photon project can move your phone\'s conversation to its own line. Zeus uses one project.')
        run(ctx, [ctx.hermes() or 'hermes', '-p', ctx.profile, 'photon', 'setup', '--phone', cfg['owner']['phone']])
    env_set(ctx, 'PHOTON_ALLOWED_USERS', cfg['owner']['phone'])
    env_set(ctx, 'PHOTON_HOME_CHANNEL', cfg['owner']['phone'])
    ctx.done.append('photon')


def step_composio(ctx: Ctx, cfg: dict) -> dict:
    say('\n7. Composio: read-only Gmail for your mailbox')
    comp = shutil.which('composio') or str(ctx.home / '.local' / 'bin' / 'composio')
    if run(ctx, [comp, 'whoami'], check=False, capture=True).returncode:
        run(ctx, [comp, 'login'])
    accounts = dict(cfg.get('mail', {}).get('composio_accounts', {}) or {})
    if not accounts.get('personal'):
        run(ctx, [comp, 'link', 'gmail', '--alias', 'zeus-personal'], check=False)
        acct = _composio_gmail_account(ctx, comp)
        if acct:
            accounts['personal'] = acct
            cfg.setdefault('mail', {})['composio_accounts'] = accounts
            write(ctx, ctx.config_file, '# Zeus settings. Edit freely; run `zeus doctor` after changes.\n' + toml_dump(cfg), mode=0o600)
        else:
            say('  No connected Gmail account found yet. Mail stays off until you run `composio link gmail` and `zeus setup` again.')
    else:
        say('  already linked')
    ctx.done.append('composio')
    return cfg


def _composio_gmail_account(ctx: Ctx, comp: str) -> str | None:
    if ctx.dry_run:
        return 'ca_dry_run'
    r = run(ctx, [comp, 'connections', 'list'], check=False, capture=True)
    try:
        data = json.loads(r.stdout or '[]')
    except json.JSONDecodeError:
        return None
    items = data if isinstance(data, list) else data.get('items') or data.get('connections') or []
    gmail = [c for c in items if 'gmail' in json.dumps(c).lower() and str(c.get('status', 'ACTIVE')).upper() == 'ACTIVE']
    preferred = [c for c in gmail if c.get('alias') == 'zeus-personal'] or gmail
    return str(preferred[0].get('id')) if preferred else None


def step_keys(ctx: Ctx) -> None:
    say('\n8. Keys (kept in the profile\'s private .env)')
    if not env_has(ctx, 'ANTHROPIC_API_KEY') and not os.environ.get('ANTHROPIC_API_KEY'):
        key = ask(ctx, 'anthropic_key', '  Anthropic API key (console.anthropic.com)', secret=True)
        if key:
            env_set(ctx, 'ANTHROPIC_API_KEY', key)
        else:
            say('  No model key yet: Zeus cannot think until you add one (`zeus setup` again, or `hermes -p zeus model`).')
    if not env_has(ctx, 'AGENTMAIL_API_KEY'):
        key = ask(ctx, 'agentmail_key', '  AgentMail API key for Zeus\'s own inbox (optional, Enter to skip)', secret=True)
        if key:
            env_set(ctx, 'AGENTMAIL_API_KEY', key)
    ctx.done.append('keys')


def _existing_jobs(ctx: Ctx) -> set:
    try:
        data = json.loads((ctx.profile_dir / 'cron' / 'jobs.json').read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    jobs = data.get('jobs', data) if isinstance(data, dict) else data
    return {j.get('name') for j in jobs if isinstance(j, dict)}


SETTLE_PROMPT = ('Purchase settle check (purchase lock). The lines above list orders you placed with Place order whose result '
                 'is not confirmed yet. If there are no CHECK lines, reply exactly [SILENT]. For each CHECK line: open that '
                 'store\'s orders page in your browser and READ it only. Never press Place order, Buy now, Pay or any button '
                 'that buys. If the order is there, run: zeus-buy placed <store id> <order number>, then tell the owner in one '
                 'short line that it went through, with the total and order number. If it is not there, run: zeus-buy checked '
                 '<store id> <the CHECK number> and do exactly what that command\'s reply says.')


def step_jobs(ctx: Ctx, cfg: dict) -> None:
    say('\n9. Scheduled jobs')
    have = _existing_jobs(ctx)
    phone = cfg['owner']['phone']
    h = [ctx.hermes() or 'hermes', '-p', ctx.profile, 'cron', 'create']
    jobs = [
        ('Zeus work upkeep', ['every 1m', '--script', 'zeus_maintenance_tick.py', '--no-agent', '--deliver', 'local']),
        ('Zeus purchase settle check', ['every 5m', SETTLE_PROMPT, '--monitor-script', 'zeus_buy_settle.py', '--deliver', f'photon:{phone}']),
        ('Zeus phone sign-in links', ['every 10m', '--script', 'zeus_signin_links.py', '--no-agent', '--deliver', f'photon:{phone}']),
        ('Zeus backup small', ['0 */6 * * *', '--script', 'zeus_backup_small.sh', '--no-agent', '--deliver', 'local']),
        ('Zeus backup full', ['30 3 * * *', '--script', 'zeus_backup_full.sh', '--no-agent', '--deliver', 'local']),
        ('Zeus backup drill', ['0 4 1 * *', '--script', 'zeus_backup_drill.sh', '--no-agent', '--deliver', 'local']),
    ]
    for name, args in jobs:
        if name in have:
            continue
        run(ctx, h + ['--name', name] + args)
    ctx.done.append('jobs')


PLIST = Template('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>EnvironmentVariables</key><dict>
    <key>ZEUS_CONFIG</key><string>$config</string>
    <key>PATH</key><string>$path</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
</dict>
</plist>
''')


def step_services(ctx: Ctx, cfg: dict) -> None:
    say('\n10. Background services (launchd)')
    agents = ctx.home / 'Library' / 'LaunchAgents'
    logs = ctx.zeus_dir / 'logs'
    py = ctx.hermes_python()
    path = f'{ctx.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'
    services = {'ai.zeus.watch': [py, '-I', str(PKG / 'watch' / 'zeus_watch.py')]}
    if cfg.get('signin', {}).get('public_base'):
        services['ai.zeus.signin'] = [py, '-I', str(PKG / 'signin' / 'handoff_server.py'), '--port', str(cfg['signin'].get('port', 8851))]
    if not ctx.dry_run:
        logs.mkdir(parents=True, exist_ok=True)
    for label, argv in services.items():
        plist = agents / f'{label}.plist'
        args = ''.join(f'<string>{_xml(a)}</string>' for a in argv)
        write(ctx, plist, PLIST.substitute(label=label, args=args, config=_xml(str(ctx.config_file)), path=path,
                                           log=_xml(str(logs / f'{label}.log'))))
        uid = os.getuid()
        run(ctx, ['launchctl', 'bootout', f'gui/{uid}/{label}'], check=False, capture=True)
        run(ctx, ['launchctl', 'bootstrap', f'gui/{uid}', str(plist)])
    ctx.done.append('services')


def _xml(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def step_gateway(ctx: Ctx) -> None:
    say('\n11. Hermes gateway (receives your texts)')
    h = ctx.hermes() or 'hermes'
    if run(ctx, [h, 'gateway', 'status'], check=False, capture=True).returncode:
        run(ctx, [h, 'gateway', 'install'])
        run(ctx, [h, 'gateway', 'start'])
    else:
        run(ctx, [h, 'gateway', 'restart'])
    ctx.done.append('gateway')


def setup(ctx: Ctx) -> int:
    say('Zeus setup. Safe to run again: each step keeps what is already done.')
    step_prereqs(ctx)
    cfg = step_owner(ctx)
    step_profile(ctx, cfg)
    step_links(ctx)
    step_patches(ctx)
    step_photon(ctx, cfg)
    cfg = step_composio(ctx, cfg)
    step_keys(ctx)
    step_jobs(ctx, cfg)
    step_services(ctx, cfg)
    step_gateway(ctx)
    say('\nDone: ' + ', '.join(ctx.done) + '.\nNext: run `zeus doctor`, then text your Zeus line "hi".')
    return 0


def uninstall(ctx: Ctx) -> int:
    """Stop Zeus's services and remove its links. Keeps the profile's data, config and history."""
    uid = os.getuid()
    for label in ('ai.zeus.watch', 'ai.zeus.signin'):
        run(ctx, ['launchctl', 'bootout', f'gui/{uid}/{label}'], check=False, capture=True)
        p = ctx.home / 'Library' / 'LaunchAgents' / f'{label}.plist'
        if p.exists() and not ctx.dry_run:
            p.unlink()
    for at in [ctx.profile_dir / 'plugins' / n for n in PLUGINS] + [ctx.profile_dir / 'scripts' / n for n in JOB_SCRIPTS] + \
              [ctx.home / '.local' / 'bin' / t for t in ('zeus-buy', 'zeus-life', 'zeus-login', 'zeus-checkpoint', 'zeus-snapshot')]:
        if at.is_symlink() and not ctx.dry_run:
            at.unlink()
    say('Zeus services stopped and links removed. Your Hermes profile, data and ~/.zeus/config.toml were kept.')
    return 0
