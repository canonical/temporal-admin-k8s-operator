# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from the 1.26/edge track to the local (1.27) build."""

import json
import pathlib
import re

import jubilant
import pytest
from conftest import TEMPORAL_SERVER_APP_NAME

CHARM_SOURCE = pathlib.Path(__file__).parent.parent.parent / "src" / "charm.py"


def _workload_version() -> str:
    """Read WORKLOAD_VERSION from the charm source without importing ops.

    Returns:
        The WORKLOAD_VERSION constant declared in src/charm.py.
    """
    match = re.search(r'^WORKLOAD_VERSION = "([^"]+)"', CHARM_SOURCE.read_text(), re.MULTILINE)
    assert match, "WORKLOAD_VERSION not found in src/charm.py"
    return match.group(1)


def _published_admin_data(juju: jubilant.Juju) -> dict:
    """Return the data admin publishes on the admin relation, as seen by the server.

    Args:
        juju: Juju object (jubilant)

    Returns:
        The remote application databag of the admin relation.
    """
    unit = f"{TEMPORAL_SERVER_APP_NAME}/0"
    unit_info = json.loads(juju.cli("show-unit", unit, "--format", "json"))
    for relation in unit_info[unit]["relation-info"]:
        if relation["endpoint"] == "admin":
            return relation.get("application-data", {})
    return {}


@pytest.mark.skip(reason="Temporarily skipped in CI.")
def test_refresh_from_1_26_to_current(juju: jubilant.Juju, admin_tools_previous_track, charm_path, charm_resources):
    """Refresh temporal-admin-k8s from 1.26/edge to the locally built charm.

    The outcome checked is the schema migration: admin must go active and
    publish readiness for this charm's workload version.
    """
    juju.refresh(
        admin_tools_previous_track,
        path=charm_path,
        resources=charm_resources,
        base="ubuntu@24.04",
    )
    juju.wait(lambda status: jubilant.all_active(status, "temporal-admin-k8s"), error=jubilant.any_error)

    expected_version = _workload_version()
    juju.wait(
        lambda _: _published_admin_data(juju).get("migrated_workload_version") == expected_version,
        error=jubilant.any_error,
        delay=5,
        timeout=300,
    )

    data = _published_admin_data(juju)
    assert data.get("schema_status") == "ready"
    assert data.get("migrated_workload_version") == expected_version
