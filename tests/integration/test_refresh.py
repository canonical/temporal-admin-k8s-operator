# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from the latest supported release to the newer charm."""

import json
import pathlib
import re

import jubilant
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


def test_refresh_from_1_30_to_1_31(
    juju: jubilant.Juju,
    admin_tools_previous_track,
    charm_path,
    charm_resources,
):
    """Test refreshing Temporal Admin from 1.30/edge to the local 1.31 build."""
    juju.refresh(
        admin_tools_previous_track,
        path=charm_path,
        resources=charm_resources,
        base="ubuntu@26.04",
    )

    juju.wait(
        lambda status: jubilant.all_active(status, "temporal-admin-k8s"),
        error=jubilant.any_error,
        timeout=600,
    )

    expected_version = _workload_version()

    juju.wait(
        lambda _: (_published_admin_data(juju).get("migrated_workload_version") == expected_version),
        error=jubilant.any_error,
        delay=5,
        timeout=300,
    )

    data = _published_admin_data(juju)
    assert data.get("schema_status") == "ready"
    assert data.get("migrated_workload_version") == expected_version
