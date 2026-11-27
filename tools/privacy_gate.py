#!/usr/bin/env python3
"""Privacy gate: fail if any file in the tree holds personal details.

Checks every text file for: home-folder paths, phone numbers, email addresses outside the allowlist,
and (with --terms FILE) any word from a private term list kept outside the repository.

    python3 tools/privacy_gate.py [--terms ~/private/terms.txt] [--only PATH ...] [--quiet]

Exit 0 = clean. Exit 1 = hits (printed as file:line: category 'match').
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Public sender and example domains that may appear in code (store receipts, calendar, test data).
ALLOWED_EMAIL_DOMAINS = re.compile(
    r'@(?:[\w-]+\.)*(?:example\.(?:test|com|org)|[\w-]+\.test|amazon\.com|walmart\.com|samsclub\.com|'
    r'google\.com|calendar\.google\.com|apple\.com|icloud\.com|agentmail\.to|noreply\.github\.com|anthropic\.com)$', re.I)
CHECKS = {
    'home-path': re.compile(r'/Users/(?!<|\$|\{)[A-Za-z0-9._-]+|/home/(?!<|\$|\{)[a-z][a-z0-9._-]+'),
    'phone': re.compile(r'(?<![\w.:/-])\+?1?[ (.-]?\d{3}[) .-]?\d{3}[ .-]\d{4}(?![\w])|\+1\d{10}\b'),
    'email': re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
}
FAKE_PHONE = re.compile(r'555[ .-]?01\d\d|\+1555')
ALLOW_MARK = 'privacy-gate: allow'   # a line deliberately naming the copyright holder
SKIP_DIRS = {'.git', '__pycache__', '.venv', 'dist', 'build'}
SKIP_FILES = {'privacy_gate.py', 'LICENSE'}   # LICENSE names the copyright holder on purpose


def load_terms(path: str | None) -> list[re.Pattern]:
    if not path:
        return []
    out = []
    for line in Path(path).expanduser().read_text().splitlines():
        t = line.strip()
        if t and not t.startswith('#'):
            out.append(re.compile(r'(?<![A-Za-z0-9])' + re.escape(t).replace(r'\ ', r'[\s_-]?') + r'(?![A-Za-z0-9])', re.I))
    return out


def scan(paths: list[Path], terms: list[re.Pattern]) -> list[str]:
    hits = []
    for base in paths:
        files = [base] if base.is_file() else [Path(r) / n for r, ds, ns in os.walk(base)
                                              for n in ns if not (ds.__setitem__(slice(None), [d for d in ds if d not in SKIP_DIRS]))]
        for f in files:
            if f.name in SKIP_FILES:
                continue
            try:
                text = f.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            rel = f.relative_to(ROOT) if f.is_relative_to(ROOT) else f
            for n, line in enumerate(text.splitlines(), 1):
                if ALLOW_MARK in line:
                    continue
                for cat, rx in CHECKS.items():
                    for m in rx.finditer(line):
                        s = m.group(0)
                        if cat == 'email' and ALLOWED_EMAIL_DOMAINS.search(s):
                            continue
                        if cat == 'phone' and FAKE_PHONE.search(s):
                            continue
                        hits.append(f'{rel}:{n}: {cat}')
                for rx in terms:
                    if rx.search(line):
                        hits.append(f'{rel}:{n}: private-term')
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--terms')
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()
    paths = [Path(p).resolve() for p in a.only] if a.only else [ROOT]
    hits = scan(paths, load_terms(a.terms))
    if not a.quiet:
        print('\n'.join(hits))
    print(f'privacy gate: {len(hits)} hit(s)', file=sys.stderr)
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
