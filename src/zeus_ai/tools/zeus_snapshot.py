"""zeus-snapshot: keep every version of Zeus (its Hermes profile) and put one back.

A version of Zeus = its settings (config.yaml, SOUL.md, profile.yaml, memories), the real code of
every plugin (links are followed, because builds change some plugin folders in place), its repeating
scheduled jobs (one-off errand jobs are work, not shape), the life engine's code, switches and
settings, and the Hermes code version plus its local patches.

Each version is a commit in a private local git store (never pushed anywhere).

  zeus-snapshot take [-m why]       save a version now (skips when nothing changed)
  zeus-snapshot list [-n 20]        newest versions first
  zeus-snapshot show ID             what changed in that version
  zeus-snapshot diff ID [ID2] [--path P]  full difference (ID2 defaults to the newest version)
  zeus-snapshot restore ID          dry run: what putting Zeus back to ID would change
  zeus-snapshot restore ID --apply  do it (saves a pre-restore version first)
  zeus-snapshot export ID PATH DEST copy one part of a version to a folder

Overrides for tests: ZEUS_PROFILE, ZEUS_SNAP_STORE, ZEUS_LIFE_ENGINE, ZEUS_HERMES_CODE.
"""
from __future__ import annotations

import argparse, datetime as dt, fcntl, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import settings  # noqa: E402

HOME = Path.home()
PROFILE = Path(os.environ.get("ZEUS_PROFILE") or settings.PROFILE_DIR)
STORE = Path(os.environ.get("ZEUS_SNAP_STORE")
             or settings.get("snapshot.store", str(settings.HERMES_HOME / "snapshots" / settings.PROFILE)))
HERMES_CODE = Path(os.environ.get("ZEUS_HERMES_CODE") or settings.HERMES_AGENT_DIR)
LIFE_LAUNCHER = Path(settings.get("engine.launcher", str(HOME / ".local/bin/zeus-life")))
RESTORED = STORE.parent / (STORE.name + "-restored")

PROFILE_FILES = ["config.yaml", "SOUL.md", "profile.yaml", "channel_aliases.json",
                 "memories/MEMORY.md", "memories/USER.md"]
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", "data", "logs", "state"}
SKIP_SUFFIX = (".pyc", ".sqlite3", ".sqlite", ".db", ".db-wal", ".db-shm", ".lock", ".log")
# life-engine workspace files that change on every tick or hold run state, not Zeus's shape
LIFE_NOISE = re.compile(r"(^last-|heartbeat|health\.json$|-state\.json$|^tick\.lock$|\.jsonl$|"
                        r"^acted-check\.json$|^signin-handoff.*\.json$|^signins\.json$|^refills\.json$|"
                        r"^watches\.json$|\.sqlite3|\.lock$)")
# keep only the fields that say what a repeating job IS, not when it last ran
JOB_KEYS = ["id", "name", "schedule", "schedule_display", "enabled", "paused_reason", "prompt",
            "deliver", "failure_deliver", "model", "provider", "base_url", "skill", "skills",
            "enabled_toolsets", "script", "no_agent", "workdir", "context_from", "monitor_script",
            "monitor_url"]
SECRET_HINT = re.compile(rb"(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|xox[abp]-[0-9A-Za-z-]{20,}|"
                         rb"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY)")


def life_engine() -> Path | None:
    if os.environ.get("ZEUS_LIFE_ENGINE"):
        return Path(os.environ["ZEUS_LIFE_ENGINE"])
    try:
        m = re.search(r"(/\S+)/lifecli\.py", LIFE_LAUNCHER.read_text())
        if m:
            return Path(m.group(1))
    except OSError:
        pass
    return settings.ENGINE_DIR if (settings.ENGINE_DIR / "lifecli.py").exists() else None


def git(*args, check=True, capture=True, cwd=None):
    r = subprocess.run(["git", "-C", str(cwd or STORE), *args], text=True,
                       capture_output=capture, check=False)
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout if capture else ""


def ensure_store():
    if (STORE / ".git").is_dir():
        return
    STORE.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE.parent, 0o700)
    os.chmod(STORE, 0o700)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "zeus-snapshot")
    git("config", "user.email", "zeus-snapshot@localhost")
    git("config", "commit.gpgsign", "false")
    (STORE / "README.md").write_text(
        "Every version of Zeus (its Hermes profile). Made by zeus-snapshot.\n"
        "Local only. Never push this repository anywhere.\n")


def skip(p: Path) -> bool:
    return p.name in SKIP_DIRS or p.name.endswith(SKIP_SUFFIX) or p.name.startswith(".env")


def copy_tree(src: Path, dst: Path):
    """Copy src (following links) into dst, leaving out caches, data stores, logs and env files."""
    src = src.resolve()
    for root, dirs, files in os.walk(src, followlinks=True):
        r = Path(root)
        dirs[:] = sorted(d for d in dirs if not skip(r / d))
        for f in sorted(files):
            p = r / f
            if skip(p) or not p.is_file():
                continue
            out = dst / p.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def fingerprint() -> str:
    """Cheap check: sizes and change times of everything a snapshot reads."""
    h = hashlib.sha256()
    paths = [PROFILE / f for f in PROFILE_FILES]
    h.update(json.dumps(repeating_jobs(), sort_keys=True, default=str).encode())
    roots = [PROFILE / "plugins"]
    le = life_engine()
    if le:
        roots.append(le)
    roots.append(PROFILE / "workspace/life-engine")
    for p in paths:
        try:
            st = p.stat(); h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{p}|-\n".encode())
    for root in roots:
        if not root.exists():
            continue
        for r, dirs, files in os.walk(root, followlinks=True):
            dirs[:] = sorted(d for d in dirs if not skip(Path(r) / d))
            h.update(f"D{r}|{os.path.realpath(r)}\n".encode())
            for f in sorted(files):
                p = Path(r) / f
                if skip(p) or (root.name == "life-engine" and root.parent.name == "workspace"
                               and LIFE_NOISE.search(f)):
                    continue
                try:
                    st = p.stat(); h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}\n".encode())
                except OSError:
                    pass
    try:
        h.update(subprocess.run(["git", "-C", str(HERMES_CODE), "diff", "HEAD"],
                                capture_output=True).stdout)
        h.update((HERMES_CODE / ".git/HEAD").read_bytes())
    except OSError:
        pass
    return h.hexdigest()


def repeating_jobs():
    """Repeating jobs, only the fields that say what a job IS (one-off errand jobs are work, not shape)."""
    try:
        jobs = json.loads((PROFILE / "cron/jobs.json").read_text()).get("jobs", [])
    except (OSError, ValueError) as e:
        return {"unreadable": str(e)}
    keep = [{k: j.get(k) for k in JOB_KEYS if k in j} for j in jobs
            if (j.get("repeat") or {}).get("times") is None]
    return sorted(keep, key=lambda j: str(j.get("id")))


def build(tree: Path):
    # 1. settings and memories
    for f in PROFILE_FILES:
        p = PROFILE / f
        if p.is_file():
            out = tree / "profile" / f
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
    # 2. plugins: real content plus where each link pointed
    links = {}
    pdir = PROFILE / "plugins"
    for p in sorted(pdir.iterdir()) if pdir.is_dir() else []:
        if p.name.startswith("."):
            continue
        links[p.name] = {"link": os.readlink(p) if p.is_symlink() else None,
                         "exists": p.exists()}
        if p.is_dir():
            copy_tree(p, tree / "plugins" / p.name)
    write_json(tree / "plugins/LINKS.json", links)
    # 3. repeating scheduled jobs only
    write_json(tree / "cron/repeating-jobs.json", repeating_jobs())
    # 4. life engine code, switches and settings
    le = life_engine()
    if le and le.is_dir():
        copy_tree(le, tree / "life-engine/code")
        write_json(tree / "life-engine/WHERE.json", {"code": str(le)})
    ws = PROFILE / "workspace/life-engine"
    if ws.is_dir():
        for p in sorted(ws.rglob("*")):
            rel = p.relative_to(ws)
            if p.is_file() and not any(LIFE_NOISE.search(part) or skip(Path(part)) for part in rel.parts):
                out = tree / "life-engine/workspace" / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, out)
    # 5. Hermes code version and local patches
    def hg(*a):
        return subprocess.run(["git", "-C", str(HERMES_CODE), *a], text=True,
                              capture_output=True).stdout
    write_json(tree / "hermes/VERSION.json", {
        "code": str(HERMES_CODE), "commit": hg("rev-parse", "HEAD").strip(),
        "changed_files": hg("status", "--porcelain=v1", "-uno").splitlines()})
    (tree / "hermes/local-changes.patch").write_text(hg("diff", "HEAD"))


def write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def changed_areas(names: list[str]) -> str:
    areas = []
    for n in names:
        parts = n.split("/")
        a = {"profile": lambda: parts[-1], "plugins": lambda: "plugin links" if parts[1] == "LINKS.json" else "plugin " + parts[1],
             "cron": lambda: "scheduled jobs", "hermes": lambda: "Hermes code",
             "life-engine": lambda: "life engine " + ("code" if parts[1] == "code" else "settings")
             }.get(parts[0], lambda: n)()
        if a not in areas:
            areas.append(a)
    return ", ".join(areas[:8]) + (f" (+{len(areas) - 8} more)" if len(areas) > 8 else "")


def take(reason: str, quiet: bool) -> str | None:
    ensure_store()
    lock = open(STORE / ".git/zeus-snapshot.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    (STORE / ".git/last-run").write_text(dt.datetime.now().isoformat(timespec="seconds") + "\n")  # heartbeat for any monitor
    fp_file = STORE / ".git/last-fingerprint"
    fp = fingerprint()
    if reason == "auto" and fp_file.exists() and fp_file.read_text() == fp:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "t"
        build(tree)
        for p in STORE.iterdir():
            if p.name not in (".git", "README.md"):
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        for p in tree.iterdir():
            shutil.move(str(p), STORE / p.name)
    git("add", "-A")
    names = [n for n in git("diff", "--cached", "--name-only").splitlines() if n]
    fp_file.write_text(fp)
    if not names:
        if not quiet:
            print("No change since the last version.")
        return None
    hits = [n for n in names if (STORE / n).is_file() and SECRET_HINT.search((STORE / n).read_bytes())]
    if hits:
        print("note: possible secret in " + ", ".join(hits) + " (store is local only, 700)", file=sys.stderr)
    first = not git("rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    summary = "first version" if first else changed_areas(names)
    git("commit", "-q", "-m", f"{reason}: {summary}" if reason != "auto" else summary,
        "-m", f"reason: {reason}\nfiles: {len(names)}")
    vid = git("rev-parse", "--short", "HEAD").strip()
    print(f"Saved Zeus version {vid}: {summary}")
    return vid


def cmd_list(n: int):
    ensure_store()
    out = git("log", f"-n{n}", "--date=format-local:%a %b %d %I:%M%p", "--format=%h  %ad  %s", check=False)
    print(out.strip() or "No versions yet.")


def resolve(vid: str) -> str:
    r = git("rev-parse", "--verify", "-q", vid + "^{commit}", check=False).strip()
    if not r:
        sys.exit(f"No Zeus version called {vid}. See: zeus-snapshot list")
    return r


def tree_hash(commit: str, path: str) -> str:
    return git("rev-parse", "-q", "--verify", f"{commit}:{path}", check=False).strip()


def live_tree_hash(src: Path) -> str:
    """Hash a live folder the same way git would, after the same filtering."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        copy_tree(src, t / "x")
        if not (t / "x").exists():
            return ""
        subprocess.run(["git", "init", "-q", str(t)], check=True)
        subprocess.run(["git", "-C", str(t), "add", "-A"], check=True)
        return subprocess.run(["git", "-C", str(t), "write-tree", "--prefix=x/"], text=True,
                              capture_output=True).stdout.strip()


def plan_restore(commit: str):
    """Work out every change to live Zeus, without touching anything."""
    steps, report = [], []
    for f in PROFILE_FILES:
        old = git("show", f"{commit}:profile/{f}", check=False)
        has = bool(tree_hash(commit, f"profile/{f}"))
        live = PROFILE / f
        cur = live.read_text() if live.is_file() else None
        if has and cur != old:
            steps.append(("write", live, f"profile/{f}"))
        elif not has and cur is not None:
            report.append(f"{f} exists now but not in that version (left as is)")
    links = json.loads(git("show", f"{commit}:plugins/LINKS.json"))
    pdir = PROFILE / "plugins"
    for name, info in sorted(links.items()):
        live = pdir / name
        want = tree_hash(commit, f"plugins/{name}")
        cur_link = os.readlink(live) if live.is_symlink() else None
        if not want:
            continue
        if info["link"] and Path(info["link"]).exists() and live_tree_hash(Path(info["link"])) == want:
            if cur_link != info["link"]:
                steps.append(("link", live, info["link"]))
        elif live.exists() and live_tree_hash(live) == want:
            pass
        else:
            steps.append(("unpack", live, f"plugins/{name}"))
    for live in sorted(pdir.iterdir()) if pdir.is_dir() else []:
        if live.name not in links and not live.name.startswith("."):
            steps.append(("set-aside", live, None))
    ws = PROFILE / "workspace/life-engine"
    names = git("ls-tree", "-r", "--name-only", commit, "life-engine/workspace/", check=False).split()
    for n in names:
        rel = n[len("life-engine/workspace/"):]
        live = ws / rel
        if not live.is_file() or live.read_bytes() != subprocess.run(
                ["git", "-C", str(STORE), "show", f"{commit}:{n}"], capture_output=True).stdout:
            steps.append(("write", live, n))
    kept = {n[len("life-engine/workspace/"):] for n in names}
    if ws.is_dir():
        for p in sorted(ws.rglob("*")):
            rel = p.relative_to(ws)
            if p.is_file() and str(rel) not in kept and not any(
                    LIFE_NOISE.search(part) or skip(Path(part)) for part in rel.parts):
                steps.append(("set-aside", p, None))
    for area, label in (("cron/repeating-jobs.json", "scheduled jobs"), ("life-engine/code", "life engine code"),
                        ("hermes", "Hermes code")):
        if subprocess.run(["git", "-C", str(STORE), "diff", "--quiet", commit, "HEAD", "--", area]).returncode:
            report.append(f"{label} differ from that version: not put back automatically "
                          f"(see: zeus-snapshot diff {commit[:9]} --path {area})")
    return steps, report


def describe(step) -> str:
    kind, live, src = step
    rel = str(live).replace(str(HOME), "~")
    return {"write": f"put back {rel}", "link": f"point {rel} at {src}".replace(str(HOME), "~"),
            "unpack": f"put back the saved code of {rel} (its old folder is gone or has changed)",
            "set-aside": f"move {rel} aside (not in that version)"}[kind]


def cmd_restore(vid: str, apply: bool):
    ensure_store()
    commit = resolve(vid)
    take("auto", quiet=True)  # compare against (and keep) the current live state
    steps, report = plan_restore(commit)
    short = commit[:9]
    if not steps:
        print(f"Live Zeus already matches version {short} for settings, plugins and life switches.")
    for s in steps:
        print(("" if apply else "would ") + describe(s))
    for r in report:
        print("note: " + r)
    if not apply or not steps:
        if steps:
            print(f"\nDry run. To do it: zeus-snapshot restore {short} --apply")
        return
    pre = take(f"pre-restore to {short}", quiet=True) or git("rev-parse", "--short", "HEAD").strip()
    aside = RESTORED / f"{dt.datetime.now():%Y%m%d-%H%M%S}-to-{short}"
    for kind, live, src in steps:
        if kind == "write":
            live.parent.mkdir(parents=True, exist_ok=True)
            data = subprocess.run(["git", "-C", str(STORE), "show", f"{commit}:{src}"],
                                  capture_output=True, check=True).stdout
            tmp = live.with_name(live.name + ".zeus-restore-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, live)
        elif kind in ("link", "unpack", "set-aside"):
            if live.is_symlink() or live.is_file():
                if kind == "set-aside" and not live.is_symlink():
                    (aside / "set-aside").mkdir(parents=True, exist_ok=True)
                    shutil.move(str(live), aside / "set-aside" / live.name)
                else:
                    live.unlink()
            elif live.is_dir():
                (aside / "set-aside").mkdir(parents=True, exist_ok=True)
                shutil.move(str(live), aside / "set-aside" / live.name)
            if kind == "link":
                live.symlink_to(src)
            elif kind == "unpack":
                dest = aside / "plugins" / live.name
                export(commit, src, dest)
                live.symlink_to(dest)
    take(f"restored to {short}", quiet=True)
    print(f"\nDone. Saved the state before this as {pre}; undo with: zeus-snapshot restore {pre} --apply")
    print("Plugins and settings load on the next gateway restart (shared gateway, waits for live work): "
          "hermes gateway restart")


def export(commit: str, path: str, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    arch = subprocess.run(["git", "-C", str(STORE), "archive", "--format=tar", commit, path],
                          capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest), f"--strip-components={path.rstrip('/').count('/') + 1}"],
                   input=arch, check=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zeus-snapshot", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take"); t.add_argument("-m", "--reason", default="manual")
    t.add_argument("--auto", action="store_true"); t.add_argument("--quiet", action="store_true")
    l = sub.add_parser("list"); l.add_argument("-n", type=int, default=20)
    s = sub.add_parser("show"); s.add_argument("id")
    d = sub.add_parser("diff"); d.add_argument("id"); d.add_argument("other", nargs="?", default="HEAD")
    d.add_argument("--path", action="append", default=[])
    r = sub.add_parser("restore"); r.add_argument("id"); r.add_argument("--apply", action="store_true")
    e = sub.add_parser("export"); e.add_argument("id"); e.add_argument("path"); e.add_argument("dest")
    a = ap.parse_args(argv)
    if a.cmd == "take":
        take("auto" if a.auto else a.reason, a.quiet)
    elif a.cmd == "list":
        cmd_list(a.n)
    elif a.cmd == "show":
        ensure_store(); print(git("show", "--stat", "--format=%h  %ad%n%s%n%b", resolve(a.id)))
    elif a.cmd == "diff":
        ensure_store()
        os.execvp("git", ["git", "-C", str(STORE), "diff", resolve(a.id), resolve(a.other), "--", *a.path])
    elif a.cmd == "restore":
        cmd_restore(a.id, a.apply)
    elif a.cmd == "export":
        ensure_store(); export(resolve(a.id), a.path, Path(a.dest).expanduser())
        print(f"Exported {a.path} of {a.id} to {a.dest}")


if __name__ == "__main__":
    main()
