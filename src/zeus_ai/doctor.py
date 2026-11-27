"""`zeus doctor`: check every piece Zeus needs and say in plain words what to fix. Reads only; changes nothing."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import installer as I

OK, WARN, FAIL = 'ok', 'warn', 'fail'


def _check(results: list, name: str, state: str, detail: str) -> None:
    results.append({'check': name, 'state': state, 'detail': detail})


def run_checks(ctx: I.Ctx) -> list[dict]:
    r: list[dict] = []
    st = I.prereq_status(ctx)
    _check(r, 'macOS', OK if st['macos'] else FAIL, 'Zeus 1.0 runs on macOS only' if not st['macos'] else 'yes')
    _check(r, 'Hermes Agent', OK if st['hermes'] else FAIL, st['hermes'] or f'not installed: {I.HERMES_INSTALL}')
    _check(r, 'Composio CLI', OK if st['composio'] else FAIL, st['composio'] or f'not installed: {I.COMPOSIO_INSTALL}')
    _check(r, 'Node.js and npm', OK if st['node'] and st['npm'] else FAIL, 'needed by the Photon iMessage bridge')
    _check(r, 'Google Chrome', OK if st['chrome'] else WARN, st['chrome'] or 'not found: store, flight and sign-in checks are off')

    cfg = I.load_config(ctx)
    owner = cfg.get('owner', {})
    _check(r, 'Zeus config', OK if owner.get('name') and owner.get('phone') else FAIL,
           str(ctx.config_file) if owner.get('phone') else 'missing: run `zeus setup`')
    _check(r, 'Hermes profile', OK if ctx.profile_dir.is_dir() else FAIL, str(ctx.profile_dir))
    missing = [n for n in I.PLUGINS if not (ctx.profile_dir / 'plugins' / n / 'plugin.yaml').exists()]
    _check(r, 'Zeus plugins', OK if not missing else FAIL, 'all linked' if not missing else 'missing: ' + ', '.join(missing))
    skills = [p.parent.name for p in (I.PKG / 'skills').glob('*/*/SKILL.md')
              if not (ctx.profile_dir / 'skills' / p.parent.parent.name / p.parent.name / 'SKILL.md').exists()]
    _check(r, 'Zeus skills', OK if not skills else FAIL, 'all linked' if not skills else 'missing: ' + ', '.join(skills))
    scripts = [n for n in I.JOB_SCRIPTS if not (ctx.profile_dir / 'scripts' / n).exists()]
    _check(r, 'Job scripts', OK if not scripts else FAIL, 'all linked' if not scripts else 'missing: ' + ', '.join(scripts))

    try:
        from . import hermes_patch
        ps = hermes_patch.status(ctx.hermes_agent_dir)
        todo = [p.get('name') or p.get('file') for p in ps.get('patches', []) if p.get('state') != 'applied']
        if not ps.get('ok'):
            _check(r, 'Hermes safety patches', FAIL, str(ps.get('error') or 'could not read the Hermes install'))
        elif ps.get('all_applied'):
            _check(r, 'Hermes safety patches', OK, 'applied')
        else:
            _check(r, 'Hermes safety patches', WARN if ps.get('required_applied') else FAIL,
                   'not applied: ' + ', '.join(map(str, todo)) + ' (run `zeus hermes-patch apply`)')
    except ImportError:
        _check(r, 'Hermes safety patches', FAIL, 'patch module missing from this build')

    for key, what, state in (('PHOTON_PROJECT_ID', 'Photon iMessage line', FAIL), ('PHOTON_PROJECT_SECRET', 'Photon secret', FAIL),
                             ('ANTHROPIC_API_KEY', 'Model key', FAIL), ('AGENTMAIL_API_KEY', 'Zeus inbox (optional)', WARN)):
        have = I.env_has(ctx, key) or (key == 'ANTHROPIC_API_KEY' and bool(os.environ.get(key)))
        _check(r, what, OK if have else state, 'set' if have else f'{key} not set in {ctx.profile_dir / ".env"}')
    accounts = cfg.get('mail', {}).get('composio_accounts', {})
    _check(r, 'Gmail via Composio', OK if accounts else WARN, 'linked' if accounts else 'not linked: mail reading is off')
    _check(r, 'Calendar', OK if cfg.get('calendar', {}).get('snapshot_file') else WARN,
           'configured' if cfg.get('calendar', {}).get('snapshot_file') else 'not configured: calendar features are off')

    have = I._existing_jobs(ctx)
    want = ['Zeus work upkeep', 'Zeus purchase settle check', 'Zeus phone sign-in links', 'Zeus backup small', 'Zeus backup full']
    gone = [j for j in want if j not in have]
    _check(r, 'Scheduled jobs', OK if not gone else FAIL, 'all present' if not gone else 'missing: ' + ', '.join(gone))
    watch = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/ai.zeus.watch'], capture_output=True, text=True)
    _check(r, 'Zeus watcher service', OK if watch.returncode == 0 else FAIL, 'loaded' if watch.returncode == 0 else 'not loaded: run `zeus setup`')
    health = Path(ctx.profile_dir / 'workspace' / 'life-engine' / 'health.json')
    if health.exists():
        try:
            h = json.loads(health.read_text())
            _check(r, 'Engine last run', OK if h.get('status') == 'ran' else WARN, str(h.get('when', 'unknown')))
        except (OSError, json.JSONDecodeError):
            _check(r, 'Engine last run', WARN, 'health note unreadable')
    else:
        _check(r, 'Engine last run', WARN, 'no run yet (normal right after setup)')
    return r


def doctor(ctx: I.Ctx, as_json: bool = False) -> int:
    results = run_checks(ctx)
    if as_json:
        print(json.dumps(results, indent=1))
    else:
        mark = {OK: 'OK  ', WARN: 'WARN', FAIL: 'FAIL'}
        for x in results:
            print(f'{mark[x["state"]]}  {x["check"]}: {x["detail"]}')
        fails = sum(x['state'] == FAIL for x in results)
        print(f'\n{fails} problem(s) to fix.' if fails else '\nAll required checks pass.')
    return 1 if any(x['state'] == FAIL for x in results) else 0
