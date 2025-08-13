# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for jubilant tests."""

import pathlib

import jubilant
import pytest
import yaml

POSTGRESQL_CHANNEL = "14/stable"
TEMPORAL_CHANNEL = "1.23/edge"
TEMPORAL_LEGACY_CHANNEL = "latest/stable"

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
    """Deploy temporal-admin-k8s from the latest track.

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
        base="ubuntu@22.04",
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
    """Deploy temporal-admin-k8s from the latest track."""
    deploy_temporal_stack(juju, temporal_admin_channel=TEMPORAL_LEGACY_CHANNEL)

    yield "temporal-admin-k8s"


@pytest.fixture(scope="module")
def charm_path() -> pathlib.Path:
    """Returns the absolute path of the locally built admin-tools-k8s charm."""
    charm_dir = pathlib.Path(__file__).parent.parent.parent
    charms = [p.absolute() for p in charm_dir.glob("*.charm")]
    assert charms, "*.charm not found in project root"
    assert len(charms) == 1, "More than one *.charm file found in project root, unsure which to use"
    return charms[0]


@pytest.fixture(scope="module")
def charm_resources() -> dict:
    """Resources to deploy the admin-tools-k8s locally built charm."""
    return {
        "temporal-admin-image": UPSTREAM_IMAGE_SOURCE,
    }
