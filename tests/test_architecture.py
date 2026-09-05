"""Structural claims the write-up makes, asserted rather than believed.

Every claim here is one a reviewer could otherwise only take on trust, and each
is the kind that decays silently: nobody notices the day a second module starts
importing Playwright, because everything still passes.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "cua"


def _imports(path: pathlib.Path) -> set[str]:
    """Top-level module names imported by a file, however they are written."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _python_files(*roots: pathlib.Path) -> list[pathlib.Path]:
    return [
        p
        for root in roots
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def test_only_one_module_knows_what_a_browser_is():
    """The Surface protocol is the seam. Exactly one file may cross it.

    This is the property that made pointing the core at a second target a
    config change: nothing above the seam expresses anything web-specific, so
    a desktop or terminal surface would slot in the same way.
    """
    importers = sorted(
        p.relative_to(ROOT).as_posix()
        for p in _python_files(CORE, ROOT / "service", ROOT / "scripts")
        if "playwright" in _imports(p)
    )
    assert importers == ["src/cua/surface/web.py"], (
        "Playwright leaked out of the surface layer: " + ", ".join(importers)
    )


def test_the_replay_path_does_not_import_the_model_library():
    """Replay is the production path and must never need a key.

    Checked structurally as well as at runtime (see the test that strips
    ``anthropic`` from ``sys.modules`` and replays), because the runtime check
    only covers the code one capability happens to execute.
    """
    offenders = sorted(
        p.relative_to(ROOT).as_posix()
        for p in _python_files(CORE)
        if "anthropic" in _imports(p) and "agent" not in p.parts
    )
    assert offenders == [], "the model library reached the replay path: " + ", ".join(offenders)


def test_the_core_does_not_name_a_target():
    """A target is described by config, never referenced from the core.

    Two docstring examples mention the local product by name; nothing imports
    it, reads its config, or branches on it.
    """
    named = []
    for path in _python_files(CORE):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("interface-hiring", "web-sample", "meridian-hosted"):
            if needle in text:
                named.append(f"{path.relative_to(ROOT).as_posix()} ({needle})")
    assert named == [], "the core names a specific target: " + ", ".join(named)


@pytest.mark.parametrize(
    "product",
    ["meridian-core", "meridian-hosted"],
)
def test_each_product_has_a_signal_library(product: str):
    """A capability recorded with no error model is weaker and says so."""
    assert (ROOT / "config" / "signals" / f"{product}.yaml").is_file()


def test_the_wrapper_does_not_reach_around_the_replay_engine():
    """The API, chatbot and dashboard must have no path that skips the gate.

    ``service/runner.py`` is the single place allowed to construct a replay;
    if another module in the wrapper builds its own, the guardrail stops being
    the only way in.
    """
    # Parsed, not grepped: api.py names ReplayEngine in its docstring to
    # explain the very property this asserts, and a text search calls that a
    # violation.
    def constructs_a_replay(path: pathlib.Path) -> bool:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "ReplayEngine"
            for n in ast.walk(tree)
        )

    builders = sorted(
        p.relative_to(ROOT).as_posix()
        for p in _python_files(ROOT / "service")
        if constructs_a_replay(p)
    )
    assert builders == ["service/runner.py"], (
        "a second module builds a replay: " + ", ".join(builders)
    )


def test_every_hosted_artifact_has_its_discovery_evidence():
    """Each recorded capability keeps the run that produced it.

    Evidence is pruned between sessions; this is what stops the prune from
    quietly removing the provenance of a capability that is still shipped.
    """
    import json

    runs = ROOT / "evidence" / "runs"
    recordings = sorted(d.name for d in runs.iterdir() if "discovery" in d.name)
    artifacts = sorted((ROOT / "artifacts").glob("meridian_hosted*.json"))
    assert artifacts, "no hosted artifacts to check"

    # One discovery recording per capability, at minimum -- the mapping itself
    # is by timestamp until every artifact carries its run id in the evidence.
    plain = [n for n in recordings if not n.endswith("verify")]
    assert len(plain) >= len(artifacts), (
        f"{len(artifacts)} hosted capabilities but only {len(plain)} discovery "
        "recordings kept; a capability has lost its provenance"
    )

    for path in artifacts:
        provenance = json.loads(path.read_text(encoding="utf-8"))["provenance"]
        assert provenance.get("discovery_run_id"), f"{path.name} records no run id"
        assert provenance.get("transcript_digest"), f"{path.name} records no transcript digest"
