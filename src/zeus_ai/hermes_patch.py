"""Apply, check and undo Zeus's patches to a Hermes Agent checkout.

Zeus's safety features (quiet-hours hold of scheduled and sent messages, the helper-result delivery fix, and
others) need small changes inside Hermes itself. They live as plain ``git diff`` files in ``hermes_patches/``,
listed in order in ``hermes_patches/series.json``. This module only ever runs ``git apply`` on them:

    status(hermes_dir)   what state each patch is in (applied, not applied, or conflicting)
    check(hermes_dir)    whether ``apply`` would succeed, without changing anything
    apply(hermes_dir)    apply every patch that is not applied yet (already-applied ones are skipped)
    revert(hermes_dir)   undo every applied patch, newest first

Every function returns a plain dict. Nothing is printed except by ``main()``. Standard library only.

Safety rules:
- Before touching anything, every selected patch is checked. If any of them neither applies cleanly nor is already
  applied, nothing is changed and the result says which patch and file is in the way.
- ``--3way`` (a merge against the patch's recorded base) is tried only when the target is a git checkout and the
  files the patch touches have no uncommitted changes, so a user's own edits are never merged over.
- If a patch fails part-way through ``apply``, the patches applied in that same run are undone again.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PATCH_DIR = Path(__file__).resolve().parent / "hermes_patches"
SERIES = PATCH_DIR / "series.json"

APPLIED, NOT_APPLIED, THREE_WAY, CONFLICT = "applied", "not_applied", "applies_3way", "conflict"


# ----------------------------------------------------------------------------------------------- helpers

def load_series(series_path: Path = SERIES) -> Dict[str, Any]:
    """The ordered patch list with its base commit."""
    data = json.loads(Path(series_path).read_text(encoding="utf-8"))
    for item in data["patches"]:
        item["path"] = str(Path(series_path).parent / item["file"])
    return data


def _git_env(hermes_dir: Path) -> Dict[str, str]:
    # Never let git discover a repository above the Hermes folder (paths in the patches are relative to it).
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(hermes_dir.parent)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(hermes_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(hermes_dir), env=_git_env(hermes_dir),
                          capture_output=True, text=True)


def _is_git_checkout(hermes_dir: Path) -> bool:
    r = _git(hermes_dir, "rev-parse", "--show-toplevel")
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == hermes_dir.resolve()


def _head(hermes_dir: Path) -> Optional[str]:
    r = _git(hermes_dir, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _patch_files(patch_path: str) -> List[str]:
    """Paths a patch touches (from its ``diff --git a/X b/X`` lines)."""
    out = []
    for line in Path(patch_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git a/"):
            out.append(line.split(" b/", 1)[1] if " b/" in line else line[13:].split(" ", 1)[0])
    return out


def _dirty(hermes_dir: Path, files: Iterable[str]) -> List[str]:
    """Files among ``files`` with uncommitted changes (git checkouts only)."""
    files = list(files)
    if not files:
        return []
    r = _git(hermes_dir, "status", "--porcelain", "--", *files)
    return sorted({line[3:].strip() for line in r.stdout.splitlines() if line.strip()}) if r.returncode == 0 else []


def _first_error(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("error:")]
    return (errors or lines or ["git apply refused the patch"])[0][:300]


def _select(series: Dict[str, Any], names: Optional[Iterable[str]], include_optional: bool) -> List[Dict[str, Any]]:
    wanted = set(names) if names else None
    chosen = []
    for item in series["patches"]:
        stem = Path(item["file"]).stem
        if wanted is not None:
            if stem in wanted or item["file"] in wanted:
                chosen.append(item)
        elif item.get("required") or include_optional:
            chosen.append(item)
    if wanted is not None:
        known = {Path(i["file"]).stem for i in series["patches"]} | {i["file"] for i in series["patches"]}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError("unknown patch name(s): " + ", ".join(unknown))
    return chosen


def _state(hermes_dir: Path, item: Dict[str, Any], git_checkout: bool) -> Dict[str, Any]:
    path = item["path"]
    row = {"patch": Path(item["file"]).stem, "file": item["file"], "required": bool(item.get("required")),
           "files": _patch_files(path)}
    reverse = _git(hermes_dir, "apply", "-R", "--check", path)
    if reverse.returncode == 0:
        return {**row, "state": APPLIED}
    forward = _git(hermes_dir, "apply", "--check", path)
    if forward.returncode == 0:
        return {**row, "state": NOT_APPLIED}
    row["detail"] = _first_error(forward.stderr)
    if git_checkout:
        dirty = _dirty(hermes_dir, row["files"])
        if dirty:
            return {**row, "state": CONFLICT, "dirty_files": dirty,
                    "detail": row["detail"] + " (these files have uncommitted changes: " + ", ".join(dirty) + ")"}
        three = _git(hermes_dir, "apply", "--3way", "--check", path)
        if three.returncode == 0:
            return {**row, "state": THREE_WAY}
    return {**row, "state": CONFLICT}


def _env_report(hermes_dir: Path, series: Dict[str, Any]) -> Dict[str, Any]:
    git_checkout = _is_git_checkout(hermes_dir)
    head = _head(hermes_dir) if git_checkout else None
    base = series["base_commit"]
    base_match = bool(head) and (head.startswith(base) or base.startswith(head[:len(base)]))
    report = {"hermes_dir": str(hermes_dir), "git_checkout": git_checkout, "head": head,
              "base_commit": base, "base_match": base_match, "messages": []}
    if git_checkout and not base_match:
        report["messages"].append(
            f"This Hermes is at {head[:12] if head else 'an unknown commit'}; the patches were made against "
            f"{base[:12]}. Patches that still apply cleanly are fine; others are tried with a 3-way merge.")
    if not git_checkout:
        report["messages"].append(
            "This Hermes folder is not a git checkout, so version checks and 3-way merges are unavailable. "
            "Patches are applied only where they match exactly.")
    return report


def _preflight(hermes_dir: Path) -> Optional[str]:
    if shutil.which("git") is None:
        return "git is not installed; it is needed to apply Hermes patches."
    if not hermes_dir.is_dir():
        return f"Hermes folder not found: {hermes_dir}"
    if not (hermes_dir / "gateway").is_dir() or not (hermes_dir / "cron").is_dir():
        return f"{hermes_dir} does not look like a Hermes Agent checkout (no gateway/ and cron/ folders)."
    return None


# ----------------------------------------------------------------------------------------------- public API

def status(hermes_dir, *, names: Optional[Iterable[str]] = None, include_optional: bool = True,
           series_path: Path = SERIES) -> Dict[str, Any]:
    """Each selected patch's state: applied, not_applied, applies_3way or conflict. Changes nothing."""
    hermes_dir = Path(hermes_dir).expanduser().resolve()
    problem = _preflight(hermes_dir)
    if problem:
        return {"ok": False, "hermes_dir": str(hermes_dir), "error": problem, "patches": []}
    series = load_series(series_path)
    report = _env_report(hermes_dir, series)
    try:
        chosen = _select(series, names, include_optional)
    except ValueError as exc:
        return {**report, "ok": False, "error": str(exc), "patches": []}
    rows = [_state(hermes_dir, item, report["git_checkout"]) for item in chosen]
    return {**report, "ok": True, "patches": rows,
            "all_applied": all(r["state"] == APPLIED for r in rows),
            "required_applied": all(r["state"] == APPLIED for r in rows if r["required"])}


def check(hermes_dir, **kwargs) -> Dict[str, Any]:
    """Would ``apply`` succeed? ``ok`` is False when any selected patch conflicts. Changes nothing."""
    st = status(hermes_dir, **kwargs)
    if not st.get("patches") and not st.get("ok"):
        return st
    conflicts = [r for r in st["patches"] if r["state"] == CONFLICT]
    msgs = list(st.get("messages", []))
    for r in conflicts:
        msgs.append(f"{r['patch']} cannot be applied: {r.get('detail', 'conflict')}")
    return {**st, "ok": not conflicts, "messages": msgs,
            "would_apply": [r["patch"] for r in st["patches"] if r["state"] in (NOT_APPLIED, THREE_WAY)],
            "already_applied": [r["patch"] for r in st["patches"] if r["state"] == APPLIED]}


def apply(hermes_dir, **kwargs) -> Dict[str, Any]:
    """Apply every selected patch that is not applied yet, in series order. All or nothing per run."""
    pre = check(hermes_dir, **kwargs)
    if not pre["ok"]:
        return {**pre, "changed": False, "applied_now": []}
    hermes_dir = Path(hermes_dir).expanduser().resolve()
    by_name = {Path(i["file"]).stem: i for i in load_series(kwargs.get("series_path", SERIES))["patches"]}
    done: List[str] = []
    for row in pre["patches"]:
        if row["state"] == APPLIED:
            continue
        item = by_name[row["patch"]]
        args = ["apply", "--3way", item["path"]] if row["state"] == THREE_WAY else ["apply", item["path"]]
        r = _git(hermes_dir, *args)
        if r.returncode != 0:
            undone = _undo(hermes_dir, [by_name[n] for n in reversed(done)])
            return {**pre, "ok": False, "changed": bool(done) and not undone["ok"], "applied_now": [],
                    "messages": pre["messages"] + [
                        f"{row['patch']} failed while applying: {_first_error(r.stderr)}",
                        "Patches applied earlier in this run were undone." if undone["ok"] else
                        "Undoing the earlier patches of this run also failed: " + "; ".join(undone["errors"])]}
        done.append(row["patch"])
    after = status(hermes_dir, names=kwargs.get("names"), include_optional=kwargs.get("include_optional", True),
                   series_path=kwargs.get("series_path", SERIES))
    return {**after, "ok": all(r["state"] == APPLIED for r in after["patches"]), "changed": bool(done),
            "applied_now": done, "messages": pre["messages"]}


def _undo(hermes_dir: Path, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors = []
    for item in items:
        r = _git(hermes_dir, "apply", "-R", item["path"])
        if r.returncode != 0:
            errors.append(f"{Path(item['file']).stem}: {_first_error(r.stderr)}")
    return {"ok": not errors, "errors": errors}


def revert(hermes_dir, *, names: Optional[Iterable[str]] = None, include_optional: bool = True,
           series_path: Path = SERIES) -> Dict[str, Any]:
    """Undo every selected applied patch, newest first. Refuses (changes nothing) if any is in a mixed state."""
    st = status(hermes_dir, names=names, include_optional=include_optional, series_path=series_path)
    if not st.get("ok"):
        return {**st, "changed": False, "reverted_now": []}
    stuck = [r for r in st["patches"] if r["state"] in (CONFLICT, THREE_WAY)]
    if stuck:
        return {**st, "ok": False, "changed": False, "reverted_now": [],
                "messages": st["messages"] + [f"{r['patch']} is neither cleanly applied nor absent; "
                                              "nothing was reverted." for r in stuck]}
    hermes_dir = Path(hermes_dir).expanduser().resolve()
    by_name = {Path(i["file"]).stem: i for i in load_series(series_path)["patches"]}
    reverted: List[str] = []
    for row in reversed(st["patches"]):
        if row["state"] != APPLIED:
            continue
        r = _git(hermes_dir, "apply", "-R", by_name[row["patch"]]["path"])
        if r.returncode != 0:
            return {**st, "ok": False, "changed": bool(reverted), "reverted_now": reverted,
                    "messages": st["messages"] + [f"{row['patch']} could not be reverted: {_first_error(r.stderr)}"]}
        reverted.append(row["patch"])
    after = status(hermes_dir, names=names, include_optional=include_optional, series_path=series_path)
    return {**after, "ok": all(r["state"] == NOT_APPLIED for r in after["patches"]), "changed": bool(reverted),
            "reverted_now": reverted, "messages": st["messages"]}


# ----------------------------------------------------------------------------------------------- debugging CLI

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m zeus_ai.hermes_patch", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["status", "check", "apply", "revert"])
    ap.add_argument("hermes_dir", nargs="?", default=str(Path.home() / ".hermes" / "hermes-agent"))
    ap.add_argument("--only", nargs="*", help="patch names (file stems) to act on")
    ap.add_argument("--required-only", action="store_true", help="skip optional patches")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    fn = {"status": status, "check": check, "apply": apply, "revert": revert}[a.action]
    result = fn(a.hermes_dir, names=a.only or None, include_optional=not a.required_only)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        for msg in result.get("messages", []):
            print("note:", msg)
        if result.get("error"):
            print("error:", result["error"])
        for row in result.get("patches", []):
            tag = "required" if row["required"] else "optional"
            print(f"{row['state']:<13} {row['patch']} ({tag})" + (f"  {row['detail']}" if row.get("detail") else ""))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
