"""Tests for the disk-cleanup plugin's deliverable-tree protection.

``reports/`` and ``scripts/`` hold DURABLE per-profile deliverables: handoff
packets, evidence, patches, cron/monitor scripts and their regression tests.
A file named ``test_*`` / ``tmp_*`` / ``*.test.*`` inside one of those trees is
a deliverable, not scratch — but the plugin classified any such name as
disposable, tracked it at session end and deleted it (``test`` is deleted at
age 0). The loss was silent: only ``disk-cleanup/cleanup.log`` recorded it.

Observed live (2026-11-17): a profile's ``scripts/test_harness_usage_report_to_cfo.py``
and ``reports/test_codex_cross_day_binding.py`` were written, run, and deleted
minutes later by the same session's own cleanup.

Regression for #107343 (``scripts/`` — a scheduled no-agent cron script and its
wrapper were deleted, so the guardrail silently degraded), #135573 and #135412
(permanent scripts deleted on non-git installs, silently).
"""

import importlib.util
import sys
import types
from datetime import datetime, UTC
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "disk-cleanup"


def _load_lib():
    """Import the plugin's library module directly from the repo path."""
    spec = importlib.util.spec_from_file_location(
        "disk_cleanup_deliverable_trees", _PLUGIN_DIR / "disk_cleanup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the plugin's ``__init__.py`` (which depends on the library)."""
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.disk_cleanup",
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.disk_cleanup"
    mod.__path__ = [str(_PLUGIN_DIR)]
    sys.modules["hermes_plugins.disk_cleanup"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """A per-test HERMES_HOME so nothing touches the real profile."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class TestDeliverableTreesNeverClassifiedDisposable:
    """A ``test_``/``tmp_``/``.test.`` name inside a deliverable tree is durable."""

    @pytest.mark.parametrize("tree", ["reports", "scripts"])
    @pytest.mark.parametrize(
        "name", ["test_deliverable.py", "tmp_deliverable.py", "deliverable.test.py"]
    )
    def test_deliverable_name_is_not_disposable(self, _isolate_env, tree, name):
        dg = _load_lib()
        target = _isolate_env / tree / name
        target.parent.mkdir(parents=True)
        target.write_text("x")
        assert dg.guess_category(target) is None

    def test_scratch_outside_deliverable_trees_is_still_disposable(self, _isolate_env):
        """Control: the protection is scoped to the deliverable trees, not the whole plugin."""
        dg = _load_lib()
        scratch = _isolate_env / "test_scratch.py"
        scratch.write_text("x")
        assert dg.guess_category(scratch) == "test"

        cache_scratch = _isolate_env / "cache" / "scratch" / "scratch.py"
        cache_scratch.parent.mkdir(parents=True)
        cache_scratch.write_text("x")
        assert dg.guess_category(cache_scratch) == "temp"

    def test_empty_dir_sweep_skips_deliverable_trees(self, _isolate_env):
        """Empty subdirs inside a deliverable tree are meaningful and must survive."""
        dg = _load_lib()
        keep = _isolate_env / "reports" / "batch-1" / "data"
        keep.mkdir(parents=True)
        sweepable = _isolate_env / "pairing"
        sweepable.mkdir()

        dg._sweep_empty_dirs(_isolate_env)

        assert keep.exists(), "empty dir inside reports/ must survive the sweep"
        assert not sweepable.exists(), "unprotected empty dirs are still swept"


class TestDeliverableSurvivesSessionEnd:
    """The real hook order: write_file into a deliverable tree, then session end."""

    @pytest.mark.parametrize("tree", ["reports", "scripts"])
    def test_deliverable_written_this_session_survives_session_end(
        self, _isolate_env, tree
    ):
        pi = _load_plugin_init()
        deliverable = _isolate_env / tree / "test_deliverable.py"
        deliverable.parent.mkdir(parents=True)

        pi._on_pre_tool_call(
            tool_name="write_file",
            args={"path": str(deliverable)},
            task_id="t",
            session_id="s",
            tool_call_id="c1",
        )
        deliverable.write_text("# deliverable\n")
        pi._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(deliverable)},
            task_id="t",
            session_id="s",
            tool_call_id="c1",
        )
        pi._on_session_end(session_id="s", completed=True, interrupted=False)

        assert deliverable.exists(), (
            f"{tree}/test_deliverable.py must not be deleted by the session-end cleanup"
        )

    @pytest.mark.parametrize("tree", ["reports", "scripts"])
    def test_stale_tracked_entry_is_dropped_not_deleted(self, _isolate_env, tree):
        """A pre-fix tracked.json entry (category "test") is dropped by re-validation."""
        dg = _load_lib()
        stale = _isolate_env / tree / "test_older.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("x")
        dg.save_tracked(
            [
                {
                    "path": str(stale),
                    "category": "test",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "size": 1,
                }
            ]
        )

        result = dg.quick()

        assert stale.exists(), (
            "a deliverable must never be deleted, not even from a stale entry"
        )
        assert result["deleted"] == 0
        assert dg.load_tracked() == [], (
            "the stale entry is dropped, not retried every session"
        )
