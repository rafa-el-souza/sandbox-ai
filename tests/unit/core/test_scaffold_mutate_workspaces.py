# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for `core.scaffold.mutate_workspaces` — round-trip and parse-error guards.

Pins the design's "tomlkit cosmetic changes" risk in two layers:

1. **Identity round-trip**: parse + dump with no edits MUST be byte-identical
   for fixtures containing the cosmetic-risk features the design enumerates
   (single/double-quoted strings, integer-valued float `4.0`, section-attached
   trailing comment, bare key, dotted key). If a `tomlkit` upgrade ever
   breaks this, this test fails before it can corrupt operator files.
2. **Mutation contract**: the mutator preserves all content outside
   `[workspaces.*]` (top-level comments, inter-section blank lines,
   section-attached comments, non-contiguous layouts) and the post-mutation
   text re-parses through both `tomllib` and `InstanceConfig.model_validate`.

Plus a parse-error fallback: malformed input → `SandboxExecutionError` and
the file on disk is unmodified.
"""

import tomllib
from pathlib import Path

import pytest
import tomlkit
from core.exceptions import SandboxExecutionError
from core.hydration import InstanceConfig
from core.scaffold import WorkspaceSpec, mutate_workspaces


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _outside_workspaces_lines(text: str) -> list[str]:
    """Return non-blank lines that are outside the `[workspaces.*]` table set.

    Used by the mutation-contract assertion that operator content (comments,
    other sections) is never lost. Blank-line counts adjacent to the
    workspaces region MAY change by ±1 across a rename per design.md
    "Risks / Trade-offs"; this filter intentionally ignores blank lines.
    """
    out: list[str] = []
    in_ws = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("[workspaces."):
            in_ws = True
            continue
        if in_ws and stripped.startswith("[") and not stripped.startswith("[workspaces."):
            in_ws = False
        if in_ws:
            continue
        if line.strip() == "":
            continue
        out.append(line)
    return out


class TestIdentityRoundTrip:
    """Layer 1: ensures `tomlkit.parse → tomlkit.dumps` with no edits is byte-identical."""

    def test_cosmetic_features_round_trip_byte_identical(self) -> None:
        # Single-quoted, double-quoted, float `4.0`, section-attached trailing comment,
        # bare key, dotted key — the cosmetic-risk surface the design enumerates.
        fixture = (
            "# top-level comment\n"
            "\n"
            "[instance]\n"
            "name = 'foo'  # section-attached comment\n"
            'host_uid = "1000"\n'
            "\n"
            "[core]\n"
            "cpus = 4.0\n"
            "bare_key = 1\n"
            "dotted.key = 2\n"
        )
        doc = tomlkit.parse(fixture)
        assert tomlkit.dumps(doc) == fixture


class TestMutationPreservesNonWorkspaceContent:
    def test_top_level_comment_survives_rename(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        original = (
            "# OPERATOR EDIT: do not remove\n"
            "\n"
            "[instance]\n"
            'name = "foo"\n'
            'host_uid = "1000"\n'
            "\n"
            "[workspaces.a]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/a"\n'
            "\n"
            "[core]\n"
            "cpus = 4.0\n"
        )
        _write(toml, original)

        # Rename `a` → `a2` is modeled as: replace the workspace set with a2 only.
        mutate_workspaces(
            str(tmp_path),
            [WorkspaceSpec(name="a2", bootstrap_mode="empty", source=None, path="/x/a")],
        )

        post = toml.read_text()
        assert "# OPERATOR EDIT: do not remove" in post
        assert "[workspaces.a2]" in post
        assert "[workspaces.a]\n" not in post
        # Comment position relative to [core] preserved.
        assert post.index("# OPERATOR EDIT") < post.index("[core]")

    def test_inter_section_blank_lines_and_comments_survive_remove(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        original = (
            "[instance]\n"
            'name = "foo"\n'
            "\n"
            "# section comment for core\n"
            "[core]\n"
            "cpus = 4.0\n"
            "\n"
            "[workspaces.keep]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/keep"\n'
            "\n"
            "[workspaces.gone]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/gone"\n'
        )
        _write(toml, original)

        mutate_workspaces(
            str(tmp_path),
            [WorkspaceSpec(name="keep", bootstrap_mode="empty", source=None, path="/x/keep")],
        )

        post = toml.read_text()
        assert "# section comment for core" in post
        assert "[workspaces.gone]" not in post
        # Blank line between [instance] and [core] preserved.
        assert "\n\n# section comment for core\n[core]" in post

    def test_non_contiguous_workspaces_tolerated_on_add(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        original = (
            "[instance]\n"
            'name = "foo"\n'
            "\n"
            "[workspaces.a]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/a"\n'
            "\n"
            "[core]\n"
            "cpus = 4.0\n"
            "\n"
            "[workspaces.b]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/b"\n'
        )
        _write(toml, original)

        mutate_workspaces(
            str(tmp_path),
            [
                WorkspaceSpec(name="a", bootstrap_mode="empty", source=None, path="/x/a"),
                WorkspaceSpec(name="b", bootstrap_mode="empty", source=None, path="/x/b"),
                WorkspaceSpec(name="c", bootstrap_mode="empty", source=None, path="/x/c"),
            ],
        )

        post = toml.read_text()
        # All three workspaces present.
        assert "[workspaces.a]" in post
        assert "[workspaces.b]" in post
        assert "[workspaces.c]" in post
        # [core] still present.
        assert "[core]" in post
        # Each workspace header appears exactly once. The pre-fix textual
        # splice (contiguous-block assumption) emitted a duplicate
        # `[workspaces.b]` — once in the rewritten block before `[core]`
        # and again in the un-spliced tail — which made the resulting
        # file unparseable by tomllib.
        assert post.count("[workspaces.a]") == 1
        assert post.count("[workspaces.b]") == 1
        assert post.count("[workspaces.c]") == 1
        # The post-mutation file must parse cleanly through tomllib;
        # duplicate workspace tables would fail with `Cannot redefine`.
        with open(toml, "rb") as f:
            tomllib.load(f)

    def test_post_mutation_parses_through_tomllib_and_pydantic(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        _write(
            toml,
            "[instance]\n"
            'name = "foo"\n'
            'host_uid = "1000"\n'
            "\n"
            "[workspaces.main]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/main"\n',
        )

        mutate_workspaces(
            str(tmp_path),
            [
                WorkspaceSpec(name="main", bootstrap_mode="empty", source=None, path="/x/main"),
                WorkspaceSpec(
                    name="scratch", bootstrap_mode="copy", source="/src", path="/x/scratch"
                ),
            ],
        )

        text = toml.read_text()
        # tomllib parse OK
        with open(toml, "rb") as f:
            tomllib.load(f)
        # InstanceConfig validation OK
        InstanceConfig.from_toml(str(toml))
        # Both workspaces preserved with their fields.
        assert 'source = "/src"' in text
        assert 'path = "/x/scratch"' in text

    def test_comments_and_non_blank_outside_lines_preserved_across_rename(self, tmp_path: Path) -> None:
        """Comments survive verbatim; every non-blank non-workspace line keeps its relative order.

        Strict byte-range equality outside `[workspaces.*]` is NOT asserted —
        tomlkit normalizes the blank-line count adjacent to the workspaces
        region by ±1 across a rename, which is an accepted cosmetic
        side-effect (see design.md "Risks / Trade-offs"). The contract that
        matters — operator content preservation — is enforced here.
        """
        toml = tmp_path / "sandbox.toml"
        original = (
            "# top comment\n"
            "[instance]\n"
            'name = "foo"\n'
            'host_uid = "1000"\n'
            "\n"
            "[workspaces.old]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/old"\n'
            "\n"
            "[core]\n"
            "cpus = 4.0\n"
        )
        _write(toml, original)
        before_lines = _outside_workspaces_lines(original)

        mutate_workspaces(
            str(tmp_path),
            [WorkspaceSpec(name="new", bootstrap_mode="empty", source=None, path="/x/new")],
        )

        after_text = toml.read_text()
        after_lines = _outside_workspaces_lines(after_text)
        # Every non-blank non-workspace line survives in the same relative order.
        assert before_lines == after_lines
        # The operator's comment is intact, byte-for-byte.
        assert "# top comment" in after_text
        # Non-workspace section ordering preserved.
        assert after_text.index("[instance]") < after_text.index("[core]")


class TestSurvivorUpdateInPlace:
    """When a workspace survives a mutation, its fields update in place."""

    def test_survivor_gains_source_when_changing_to_copy_mode(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        _write(
            toml,
            "[instance]\n"
            'name = "foo"\n'
            'host_uid = "1000"\n'
            "\n"
            "[workspaces.main]\n"
            'bootstrap_mode = "empty"\n'
            'path = "/x/main"\n',
        )

        mutate_workspaces(
            str(tmp_path),
            [WorkspaceSpec(name="main", bootstrap_mode="copy", source="/src", path="/x/main")],
        )

        text = toml.read_text()
        assert 'bootstrap_mode = "copy"' in text
        assert 'source = "/src"' in text

    def test_survivor_loses_source_when_reverting_to_empty_mode(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        _write(
            toml,
            "[instance]\n"
            'name = "foo"\n'
            'host_uid = "1000"\n'
            "\n"
            "[workspaces.main]\n"
            'bootstrap_mode = "copy"\n'
            'source = "/src"\n'
            'path = "/x/main"\n',
        )

        mutate_workspaces(
            str(tmp_path),
            [WorkspaceSpec(name="main", bootstrap_mode="empty", source=None, path="/x/main")],
        )

        text = toml.read_text()
        assert 'bootstrap_mode = "empty"' in text
        assert "source =" not in text


class TestParseErrorFallback:
    def test_malformed_toml_raises_and_leaves_file_unmodified(self, tmp_path: Path) -> None:
        toml = tmp_path / "sandbox.toml"
        original = "[instance\nname = broken"  # unclosed table header
        _write(toml, original)

        with pytest.raises(SandboxExecutionError) as exc_info:
            mutate_workspaces(
                str(tmp_path),
                [WorkspaceSpec(name="main", bootstrap_mode="empty", source=None, path="/x")],
            )

        msg = str(exc_info.value)
        assert f"Cannot mutate {toml}:" in msg
        assert f"Re-run 'sandbox status {tmp_path.name}'" in msg
        # File on disk untouched.
        assert toml.read_text() == original
