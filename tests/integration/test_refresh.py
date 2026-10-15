# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from latest track to the 1.23 track."""

import jubilant
import pytest
from conftest import POSTGRESQL_CHANNEL, TEMPORAL_CHANNEL, TEMPORAL_SERVER_APP_NAME

ADMIN_APP_NAME = "temporal-admin-k8s"

# The latest/ track was closed on Charmhub, so latest/stable can no longer be resolved.
# Revision 13 was the last one released to latest/stable and remains downloadable by revision.
TEMPORAL_ADMIN_LEGACY_CHANNEL = "latest/stable"
TEMPORAL_ADMIN_LEGACY_REVISION = 13


@pytest.fixture(scope="module")
def admin_tools_latest_track(juju: jubilant.Juju):
    """Deploy the temporal stack with temporal-admin-k8s at its last latest/stable revision."""
    juju.model_config(
        values={
            "update-status-hook-interval": "10s",
        },
    )

    juju.deploy(
        charm="postgresql-k8s",
        app="postgresql-k8s",
        channel=POSTGRESQL_CHANNEL,
        trust=True,
        base="ubuntu@22.04",
    )

    juju.deploy(
        charm="temporal-k8s",
        app=TEMPORAL_SERVER_APP_NAME,
        channel=TEMPORAL_CHANNEL,
        config={
            "num-history-shards": 1,
        },
        base="ubuntu@24.04",
    )

    juju.deploy(
        charm="temporal-admin-k8s",
        app=ADMIN_APP_NAME,
        channel=TEMPORAL_ADMIN_LEGACY_CHANNEL,
        revision=TEMPORAL_ADMIN_LEGACY_REVISION,
        base="ubuntu@22.04",
    )

    juju.integrate(f"{TEMPORAL_SERVER_APP_NAME}:db", "postgresql-k8s:database")
    juju.integrate(f"{TEMPORAL_SERVER_APP_NAME}:visibility", "postgresql-k8s:database")
    juju.integrate(f"{TEMPORAL_SERVER_APP_NAME}:admin", f"{ADMIN_APP_NAME}:admin")

    juju.wait(jubilant.all_active)

    yield ADMIN_APP_NAME


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
