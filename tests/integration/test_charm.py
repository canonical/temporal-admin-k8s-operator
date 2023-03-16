#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd Ltd.
# See LICENSE file for licensing details.

# More extensive integration tests for this charm are at
# <https://github.com/canonical/temporal-k8s-operator/blob/main/tests/integration/test_charm.py>.

import logging
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest_asyncio.fixture(name="deploy", scope="module")
async def deploy(ops_test: OpsTest):
    """The app is up and running."""
    charm = await ops_test.build_charm(".")
    resources = {"temporal-admin-image": METADATA["containers"]["temporal-admin"]["upstream-source"]}

    # Deploy temporal server, temporal admin and postgresql charms
    await ops_test.model.deploy("temporal-k8s", channel="beta")
    await ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME)
    await ops_test.model.deploy("postgresql-k8s", channel="edge", trust=True)

    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            apps=["temporal-k8s", APP_NAME], status="blocked", raise_on_blocked=False, timeout=600
        )
        await ops_test.model.wait_for_idle(
            apps=["postgresql-k8s"], status="active", raise_on_blocked=False, timeout=600
        )

        await ops_test.model.integrate("temporal-k8s:db", "postgresql-k8s:db")
        await ops_test.model.integrate("temporal-k8s:visibility", "postgresql-k8s:db")
        await ops_test.model.integrate("temporal-k8s:admin", f"{APP_NAME}:admin")

        await ops_test.juju(
            "exec",
            "--unit",
            "temporal-k8s/0",
            "--",
            "open-port",
            "7233",
        )

        await ops_test.model.wait_for_idle(apps=["temporal-k8s"], status="active", raise_on_blocked=False, timeout=600)

        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("deploy")
class TestDeployment:
    """Integration tests for Temporal admin charm."""

    async def test_tctl_action(self, ops_test: OpsTest):
        """Is it possible to run tctl command via the action."""
        action = (
            await ops_test.model.applications[APP_NAME]
            .units[0]
            .run_action("tctl", args="--ns default namespace register -rd 3")
        )
        result = (await action.wait()).results

        logger.info(f"tctl result: {result}")

        await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", raise_on_blocked=False, timeout=600)

        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"
        assert ("output" in result) and "Namespace default successfully registered" in result["output"]
