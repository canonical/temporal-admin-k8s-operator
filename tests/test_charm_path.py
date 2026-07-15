# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for built charm path resolution helpers."""

import pathlib

import pytest

from tests.charm_path import resolve_built_charm


def test_resolve_built_charm_prefers_project_root(tmp_path: pathlib.Path):
    """Prefer a charm artifact found directly in the project root."""
    root_charm = tmp_path / "temporal-admin-k8s.charm"
    nested_charm = tmp_path / "build" / "temporal-admin-k8s-nested.charm"
    nested_charm.parent.mkdir()
    root_charm.touch()
    nested_charm.touch()

    assert resolve_built_charm(tmp_path) == root_charm.absolute()


def test_resolve_built_charm_finds_nested_artifact(tmp_path: pathlib.Path):
    """Find a nested charm artifact when none exists in the project root."""
    nested_charm = tmp_path / "build" / "temporal-admin-k8s.charm"
    nested_charm.parent.mkdir()
    nested_charm.touch()

    assert resolve_built_charm(tmp_path) == nested_charm.absolute()


def test_resolve_built_charm_ignores_tox_artifacts(tmp_path: pathlib.Path):
    """Ignore charm artifacts located under tox-managed directories."""
    ignored_charm = tmp_path / ".tox" / "integration" / "temporal-admin-k8s.charm"
    ignored_charm.parent.mkdir(parents=True)
    ignored_charm.touch()

    with pytest.raises(AssertionError, match=r"\*\.charm not found under project root"):
        resolve_built_charm(tmp_path)


def test_resolve_built_charm_rejects_multiple_nested_artifacts(tmp_path: pathlib.Path):
    """Reject ambiguous results when multiple nested charm artifacts are present."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "temporal-admin-k8s-a.charm").touch()
    (build_dir / "temporal-admin-k8s-b.charm").touch()

    with pytest.raises(AssertionError, match="More than one"):
        resolve_built_charm(tmp_path)
