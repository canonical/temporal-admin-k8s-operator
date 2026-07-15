# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for locating built charm artifacts in tests."""

import pathlib

IGNORED_CHARM_DIRS = {".git", ".tox", "__pycache__", "venv"}


def resolve_built_charm(project_root: pathlib.Path) -> pathlib.Path:
    """Return the path to the built charm for this repository.

    Args:
        project_root: Repository root to search for built charm artifacts.

    Returns:
        Absolute path to the single built charm artifact.
    """
    charms = sorted(path.absolute() for path in project_root.glob("*.charm"))
    assert len(charms) <= 1, "More than one *.charm file found in project root, unsure which to use"
    if charms:
        return charms[0]

    charms = sorted(
        path.absolute() for path in project_root.rglob("*.charm") if IGNORED_CHARM_DIRS.isdisjoint(path.parts)
    )
    assert charms, "*.charm not found under project root"
    assert len(charms) == 1, "More than one *.charm file found under project root, unsure which to use"
    return charms[0]
