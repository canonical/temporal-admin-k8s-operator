# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from latest track to the 1.23 track."""

import jubilant
from conftest import TEMPORAL_SERVER_APP_NAME


def test_refresh_from_latest_to_1_23(juju: jubilant.Juju, admin_tools_latest_track, charm_path, charm_resources):
    """Test to refresh from latest track to the 1.23 track."""
    juju.refresh(
        admin_tools_latest_track,
        path=charm_path,
        resources=charm_resources,
        base="ubuntu@24.04",
    )

    juju.integrate(
        f"{TEMPORAL_SERVER_APP_NAME}:temporal-host-info",
        f"{admin_tools_latest_track}:temporal-host-info",
    )

    juju.wait(jubilant.all_active, error=jubilant.any_error)
