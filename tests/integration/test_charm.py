#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd Ltd.
# See LICENSE file for licensing details.

# More extensive integration tests for this charm are at
# <https://github.com/canonical/temporal-k8s-operator/blob/main/tests/integration/test_charm.py>.

import logging
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.fixture(name="deploy", scope="module")
async def deploy(ops_test: OpsTest):
    charm = await ops_test.build_charm(".")
    resources = {"temporal-admin-image": METADATA["resources"]["temporal-admin-image"]["upstream-source"]}
    await ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME)
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status="blocked", raise_on_blocked=False, timeout=1000)
        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("deploy")
class TestDeployment:
    async def test_tctl_action(self, ops_test: OpsTest):
        """Is it possible to run tctl command via the action."""
        # TODO(frankban): implement this test.
