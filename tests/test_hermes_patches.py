"""The Hermes patch set is complete, ordered and well formed; the patch tool refuses a non-Hermes folder."""
import json
from pathlib import Path

from zeus_ai import hermes_patch

PATCHES = Path(hermes_patch.__file__).resolve().parent / "hermes_patches"


def test_series_lists_every_patch_once_in_order():
    series = json.loads((PATCHES / "series.json").read_text())
    files = [p["file"] for p in series["patches"]]
    assert files == sorted(files) and len(files) == len(set(files))
    assert sorted(files) == sorted(p.name for p in PATCHES.glob("*.patch"))
    assert len(series["base_commit"]) == 40
    assert any(p["required"] for p in series["patches"])


def test_every_patch_is_a_git_diff():
    for patch in PATCHES.glob("*.patch"):
        assert patch.read_text().startswith("diff --git a/"), patch.name


def test_tool_refuses_a_folder_that_is_not_hermes(tmp_path):
    for fn in (hermes_patch.status, hermes_patch.check, hermes_patch.apply, hermes_patch.revert):
        result = fn(tmp_path)
        assert result["ok"] is False and "Hermes" in (result.get("error") or "")
    assert list(tmp_path.iterdir()) == []
