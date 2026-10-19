# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from the latest supported release to the newer charm."""

import jubilant
import pytest
from conftest import TEMPORAL_SERVER_APP_NAME


def test_refresh_from_1_30_to_1_31(
    juju: jubilant.Juju,
    admin_tools_latest_track,
    charm_path,
    charm_resources,
):
    """Refresh admin from 1.30/edge to the local 1.31 build."""
    juju.refresh(
        admin_tools_latest_track,
        path=charm_path,
        resources=charm_resources,
        base="ubuntu@26.04",
    )

    juju.wait(
    lambda status: jubilant.all_active(status, "temporal-admin-k8s"),
    error=jubilant.any_error,
    timeout=600,
)
