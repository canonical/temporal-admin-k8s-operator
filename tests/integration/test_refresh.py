# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from the latest supported release to the newer charm."""

import json
import pathlib
import re

import jubilant
from conftest import TEMPORAL_SERVER_APP_NAME

CHARM_SOURCE = pathlib.Path(__file__).parent.parent.parent / "src" / "charm.py"

@pytest.mark.skip(reason="Skipping because of canonical/temporal-k8s-operator/issues/150")
def test_refresh_from_latest_release(juju: jubilant.Juju, admin_tools_latest_track, charm_path, charm_resources):
    """Refresh from the latest supported temporal-admin-k8s release to the local build."""
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
