# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for jubilant tests."""

import pathlib

import jubilant
import pytest
import pytest_asyncio
import yaml
from pytest_operator.plugin import OpsTest

POSTGRESQL_CHANNEL = "14/stable"

# Temporal charm channels. Bump these when the charms migrate to a new track
# (e.g. 1.23 -> 1.31).
TEMPORAL_CHANNEL = "1.31/edge"  # server dependency and the default admin deploy
TEMPORAL_ADMIN_LATEST_RELEASE_CHANNEL = "1.31/stable"  # published admin release the refresh test upgrades from

TEMPORAL_SERVER_APP_NAME = "temporal-k8s"

METADATA = yaml.safe_load(pathlib.Path("./metadata.yaml").read_text())
UPSTREAM_IMAGE_SOURCE = METADATA["resources"]["temporal-admin-image"]["upstream-source"]


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    keep_models = bool(request.config.getoption("--keep-models"))

    with jubilant.temp_model(keep=keep_models) as model:
        model.wait_timeout = 10 * 60

        yield model

        if request.session.testsfailed:
            log = model.debug_log(limit=1000)
            print(log, end="")


def deploy_temporal_stack(
    juju: jubilant.Juju,
    postgresql_channel: str = POSTGRESQL_CHANNEL,
    temporal_channel: str = TEMPORAL_CHANNEL,
    temporal_admin_channel: str = TEMPORAL_CHANNEL,
):
    """Deploy the temporal stack.

    Args:
        juju: Juju object (jubilant)
        postgresql_channel: channel of postgresql-k8s charm
        temporal_channel: channel of temporal-k8s charm
        temporal_admin_channel: channel of temporal-admin-k8s charm
    """
    juju.model_config(
        values={
            "update-status-hook-interval": "10s",
        },
    )

    juju.deploy(
        charm="postgresql-k8s",
        app="postgresql-k8s",
        channel=postgresql_channel,
        trust=True,
        base="ubuntu@22.04",
    )

    juju.deploy(
        charm="temporal-k8s",
        app="temporal-k8s",
        channel=temporal_channel,
        config={
            "num-history-shards": 1,
        },
        base="ubuntu@24.04",
    )

    juju.deploy(
        charm="temporal-admin-k8s",
        app="temporal-admin-k8s",
        channel=temporal_admin_channel,
        base="ubuntu@22.04",
    )

    juju.integrate("temporal-k8s:db", "postgresql-k8s:database")
    juju.integrate("temporal-k8s:visibility", "postgresql-k8s:database")

    juju.integrate("temporal-k8s:admin", "temporal-admin-k8s:admin")

    juju.wait(jubilant.all_active)


@pytest.fixture(scope="module")
def admin_tools_latest_track(juju: jubilant.Juju):
    """Deploy the temporal stack with temporal-admin from the latest supported release.

    The refresh test then upgrades this deployment to the newer, locally built charm.
    """
    deploy_temporal_stack(juju, temporal_admin_channel=TEMPORAL_ADMIN_LATEST_RELEASE_CHANNEL)

    yield "temporal-admin-k8s"


@pytest_asyncio.fixture(scope="module")
async def charm_path(request: pytest.FixtureRequest, ops_test: OpsTest) -> str | pathlib.Path:
    """Build (or locate via --charm-file) the admin-tools-k8s charm and return its path.

    Uses pytest-operator's build_charm so the artifact is managed the same way as
    the rest of the integration suite. Relying on a pre-packed charm in the project
    root or build/ does not work: pytest-operator's build_charm relocates root
    *.charm files and deletes the build/ directory.
    """
    if charms := request.config.getoption("--charm-file"):
        return charms[0]
    charm = await ops_test.build_charm(".")
    assert charm, "Charm not built"
    return charm


@pytest.fixture(scope="module")
def charm_resources() -> dict:
    """Resources to deploy the admin-tools-k8s locally built charm."""
    return {
        "temporal-admin-image": UPSTREAM_IMAGE_SOURCE,
    }
