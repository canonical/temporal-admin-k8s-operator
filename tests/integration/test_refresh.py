# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from latest track to the 1.23 track."""

import logging

import jubilant
from conftest import TEMPORAL_SERVER_APP_NAME

logger = logging.getLogger(__name__)


def test_refresh_from_latest_to_1_23(juju: jubilant.Juju, admin_tools_latest_track, charm_path, charm_resources):
    """Test to refresh from latest track to the 1.23 track."""
    juju.refresh(
        admin_tools_latest_track,
        path=charm_path,
        resources=charm_resources,
    )

    try:
        juju.integrate(
            f"{TEMPORAL_SERVER_APP_NAME}:temporal-host-info",
            f"{admin_tools_latest_track}:temporal-host-info",
        )
    except jubilant.CLIError as exc:
        # During upgrade testing, temporal-k8s may still be on a legacy revision
        # that does not expose temporal-host-info. In that case, use deprecated
        # server-name as a compatibility fallback.
        if "temporal-host-info" in str(exc) and "has no" in str(exc):
            juju.cli("config", admin_tools_latest_track, f"server-name={TEMPORAL_SERVER_APP_NAME}")
        else:
            raise

    juju.wait(jubilant.all_active, error=jubilant.any_error)
