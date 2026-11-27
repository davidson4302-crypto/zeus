"""The `zeus` command."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import installer as I

PKG = Path(__file__).resolve().parent


def _ctx(a) -> I.Ctx:
    return I.Ctx(profile=a.profile, dry_run=getattr(a, 'dry_run', False), yes=getattr(a, 'yes', False),
                 answers=json.loads(Path(a.answers).read_text()) if getattr(a, 'answers', None) else {})


def _passthrough(script: Path, args: list[str]) -> int:
    py = os.environ.get('ZEUS_PYTHON') or I.Ctx().hermes_python()
    os.execv(py, [py, '-I', str(script), *args])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='zeus', description='Zeus: a proactive personal assistant for Hermes Agent, over iMessage.')
    ap.add_argument('--version', action='version', version=f'zeus {__version__}')
    ap.add_argument('--profile', default=os.environ.get('ZEUS_PROFILE', 'zeus'), help='Hermes profile name (default zeus)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('setup', help='install or repair Zeus (safe to run again)')
    s.add_argument('--dry-run', action='store_true', help='print what would happen, change nothing')
    s.add_argument('--yes', action='store_true', help='accept defaults, never prompt')
    s.add_argument('--answers', help='JSON file of answers (for scripted installs)')
    s.add_argument('--reset-config', action='store_true', help='rewrite config.yaml and SOUL.md from the templates (backups kept)')

    d = sub.add_parser('doctor', help='check every piece Zeus needs')
    d.add_argument('--json', action='store_true')

    p = sub.add_parser('hermes-patch', help='Zeus\'s safety patches to Hermes')
    p.add_argument('action', choices=['status', 'check', 'apply', 'revert'])

    u = sub.add_parser('uninstall', help='stop Zeus and remove its links (keeps your data)')
    u.add_argument('--dry-run', action='store_true')

    sub.add_parser('life', help='the life engine command line (same as zeus-life)', add_help=False)
    sub.add_parser('buy', help='record the owner\'s yes to one order (same as zeus-buy)', add_help=False)
    sub.add_parser('paths', help='show where Zeus keeps things')

    a, rest = ap.parse_known_args(argv)
    if a.cmd == 'life':
        return _passthrough(PKG / 'engine' / 'lifecli.py', rest)
    if a.cmd == 'buy':
        return _passthrough(PKG / 'bin' / 'zeus-buy', rest)
    if rest:
        ap.error('unrecognized arguments: ' + ' '.join(rest))
    if a.cmd == 'setup':
        ctx = _ctx(a)
        if a.reset_config:
            ctx.answers['reset_config'] = True
        return I.setup(ctx)
    if a.cmd == 'doctor':
        from .doctor import doctor
        return doctor(_ctx(a), as_json=a.json)
    if a.cmd == 'uninstall':
        return I.uninstall(_ctx(a))
    if a.cmd == 'hermes-patch':
        from . import hermes_patch
        fn = getattr(hermes_patch, a.action)
        res = fn(_ctx(a).hermes_agent_dir)
        print(json.dumps(res, indent=1, default=str))
        return 0 if res.get('ok', res.get('all_applied', True)) else 1
    if a.cmd == 'paths':
        ctx = _ctx(a)
        for k, v in {'config': ctx.config_file, 'profile': ctx.profile_dir, 'hermes': ctx.hermes_agent_dir,
                     'package': PKG, 'logs': ctx.zeus_dir / 'logs'}.items():
            print(f'{k:8s} {v}')
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
