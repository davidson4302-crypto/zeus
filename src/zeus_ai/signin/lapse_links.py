"""Text the owner a phone sign-in link when a store signed Zeus out.

Kept as an entry point for jobs that call it by this name; the logic lives in watch/zeus_signin_links.py (quiet
hours via ownertime.alert_open, back-off, the held-errand line). Run it the same way: whatever this prints is texted
to the owner word for word; nothing printed = nothing sent.
"""
import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / 'watch' / 'zeus_signin_links.py'


def _load():
    spec = importlib.util.spec_from_file_location('zeus_signin_links', _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    _load().main()


if __name__ == '__main__':
    main()
