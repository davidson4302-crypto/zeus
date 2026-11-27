"""Install-path tests: run with a fake home folder, never touch the real machine."""
import compileall
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src' / 'zeus_ai'
sys.path.insert(0, str(ROOT / 'src'))

from zeus_ai import installer as I  # noqa: E402

ANSWERS = {'name': 'Alex Rivera', 'first_name': 'Alex', 'phone': '+15555550100', 'timezone': 'America/New_York',
           'email': 'alex@example.test', 'quiet_start': '21', 'quiet_end': '9', 'anthropic_key': '', 'agentmail_key': ''}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('ZEUS_CONFIG', raising=False)
    monkeypatch.delenv('HERMES_HOME_ROOT', raising=False)
    c = I.Ctx(home=tmp_path, answers=dict(ANSWERS), yes=True)
    c.profile_dir.mkdir(parents=True)          # stands in for `hermes profile create`
    return c


def test_every_python_file_compiles():
    assert compileall.compile_dir(str(PKG), quiet=1, force=True, legacy=False)


def test_plugins_and_job_scripts_exist():
    for name in I.PLUGINS:
        assert (PKG / 'plugins' / name / 'plugin.yaml').exists(), name
    for rel in I.JOB_SCRIPTS.values():
        assert (PKG / rel).exists(), rel


def test_toml_round_trip():
    d = {'owner': {'name': 'A "quoted" name', 'emails': {'personal': 'a@example.test'}}, 'quiet_hours': {'start': 21},
         'flag': True, 'calendar': {'ics_urls': ['x', 'y']}}
    assert tomllib.loads(I.toml_dump(d)) == d


def test_owner_profile_and_links(ctx):
    cfg = I.step_owner(ctx)
    saved = tomllib.loads(ctx.config_file.read_text())
    assert saved['owner']['phone'] == '+15555550100' and saved['quiet_hours'] == {'start': 21, 'end': 9}
    assert oct(ctx.config_file.stat().st_mode & 0o777) == '0o600'
    I.step_profile(ctx, cfg)
    conf = (ctx.profile_dir / 'config.yaml').read_text()
    assert '${' not in conf and 'America/New_York' in conf and 'deliver_gate: true' in conf
    soul = (ctx.profile_dir / 'SOUL.md').read_text()
    assert 'Alex' in soul and '{' not in soul
    I.step_links(ctx)
    for name in I.PLUGINS:
        assert (ctx.profile_dir / 'plugins' / name).resolve() == (PKG / 'plugins' / name).resolve()
    assert (ctx.home / '.local' / 'bin' / 'zeus-buy').is_symlink()


def test_setup_is_idempotent_for_local_steps(ctx):
    cfg = I.step_owner(ctx)
    I.step_profile(ctx, cfg)
    (ctx.profile_dir / 'config.yaml').write_text('# my edit\n')
    I.step_profile(ctx, cfg)                       # second run keeps the user's edit
    assert (ctx.profile_dir / 'config.yaml').read_text() == '# my edit\n'
    I.step_links(ctx)
    I.step_links(ctx)


def test_env_file_is_private(ctx):
    I.env_set(ctx, 'PHOTON_ALLOWED_USERS', '+15555550100')
    I.env_set(ctx, 'PHOTON_ALLOWED_USERS', '+15555550101')
    env = ctx.profile_dir / '.env'
    assert env.read_text().count('PHOTON_ALLOWED_USERS=') == 1
    assert oct(env.stat().st_mode & 0o777) == '0o600'


def test_settings_reads_config(ctx, monkeypatch):
    I.step_owner(ctx)
    monkeypatch.setenv('ZEUS_CONFIG', str(ctx.config_file))
    code = ('import sys; sys.path.insert(0, sys.argv[1]); import settings; '
            'print(settings.OWNER_FIRST, settings.OWNER_PHONE, settings.QUIET_START, settings.PROFILE)')
    out = subprocess.run([sys.executable, '-c', code, str(PKG / 'engine')], capture_output=True, text=True, check=True).stdout
    assert out.split() == ['Alex', '+15555550100', '21', 'zeus']


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    c = I.Ctx(home=tmp_path, answers=dict(ANSWERS), yes=True, dry_run=True)
    I.step_links(c)
    assert not any(tmp_path.iterdir())


def test_cli_version_and_paths(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(ROOT / 'src'))
    v = subprocess.run([sys.executable, '-m', 'zeus_ai.cli', '--version'], capture_output=True, text=True, env=env)
    assert v.returncode == 0 and v.stdout.startswith('zeus ')
    p = subprocess.run([sys.executable, '-m', 'zeus_ai.cli', 'paths'], capture_output=True, text=True, env=env)
    assert str(tmp_path) in p.stdout


def test_doctor_on_empty_machine_reports_not_crashes(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(ROOT / 'src'))
    r = subprocess.run([sys.executable, '-m', 'zeus_ai.cli', 'doctor', '--json'], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    checks = {x['check']: x['state'] for x in json.loads(r.stdout)}
    assert checks['Zeus config'] == 'fail' and checks['Hermes profile'] == 'fail'


def test_package_passes_generic_privacy_gate():
    r = subprocess.run([sys.executable, str(ROOT / 'tools' / 'privacy_gate.py'), '--quiet'], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
