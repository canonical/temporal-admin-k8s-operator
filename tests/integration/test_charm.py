# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

# More extensive integration tests for this charm are at
# <https://github.com/canonical/temporal-k8s-operator/blob/main/tests/integration/test_charm.py>.


"""Temporal admin charm integration tests."""

import json
import logging
import time

import pytest
import pytest_asyncio
from helpers import APP_NAME, METADATA, SERVER_APP_NAME, run_tctl_action
from juju.action import Action
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture(name="deploy", scope="module")
async def deploy(ops_test: OpsTest):
    """The app is up and running."""
    charm = await ops_test.build_charm(".")
    resources = {"temporal-admin-image": METADATA["containers"]["temporal-admin"]["upstream-source"]}

    # Deploy temporal server, temporal admin and postgresql charms
    await ops_test.model.deploy(SERVER_APP_NAME, channel="edge")
    await ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME)
    await ops_test.model.deploy("postgresql-k8s", channel="14/stable", trust=True)

    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            apps=[SERVER_APP_NAME, APP_NAME], status="blocked", raise_on_blocked=False, timeout=600
        )
        await ops_test.model.wait_for_idle(
            apps=["postgresql-k8s"], status="active", raise_on_blocked=False, timeout=600
        )

        await ops_test.model.integrate("temporal-k8s:db", "postgresql-k8s:database")
        await ops_test.model.integrate("temporal-k8s:visibility", "postgresql-k8s:database")
        await ops_test.model.integrate("temporal-k8s:admin", f"{APP_NAME}:admin")

        await ops_test.model.wait_for_idle(apps=[SERVER_APP_NAME], status="active", raise_on_blocked=False, timeout=600)

        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("deploy")
class TestDeployment:
    """Integration tests for Temporal admin charm."""

    async def test_tctl_action(self, ops_test: OpsTest):
        """Is it possible to run tctl command via the action."""
        await run_tctl_action(ops_test, namespace="default")

    async def test_openfga_relation(self, ops_test: OpsTest):
        """Add OpenFGA relation and authorization model."""
        await ops_test.model.applications[SERVER_APP_NAME].set_config({"auth-enabled": "true"})
        await ops_test.model.deploy("openfga-k8s", channel="latest/edge")
        await ops_test.model.wait_for_idle(
            apps=[SERVER_APP_NAME, "openfga-k8s"],
            status="blocked",
            raise_on_blocked=False,
            timeout=1200,
        )

        logger.info("adding openfga postgresql relation")
        await ops_test.model.integrate("openfga-k8s:database", "postgresql-k8s:database")

        await ops_test.model.wait_for_idle(
            apps=["openfga-k8s"],
            status="blocked",
            raise_on_blocked=False,
            timeout=1200,
        )

        openfga_unit = ops_test.model.applications["openfga-k8s"].units[0]
        for i in range(10):
            action: Action = await openfga_unit.run_action("schema-upgrade")
            result = await action.wait()
            logger.info(f"attempt {i} -> action result {result.status} {result.results}")
            if result.results == {"result": "done", "return-code": 0}:
                break
            time.sleep(2)

        await ops_test.model.wait_for_idle(
            apps=["openfga-k8s"],
            status="active",
            raise_on_blocked=True,
            timeout=1200,
        )

        logger.info("adding openfga relation")
        await ops_test.model.integrate(SERVER_APP_NAME, "openfga-k8s")

        await ops_test.model.wait_for_idle(
            apps=[SERVER_APP_NAME],
            status="blocked",
            raise_on_blocked=False,
            timeout=600,
        )

        logger.info("running the create authorization model action")
        temporal_unit = ops_test.model.applications[SERVER_APP_NAME].units[0]
        with open("./temporal_auth_model.json", "r", encoding="utf-8") as model_file:
            model_data = model_file.read()

            # Remove whitespace and newlines from JSON object
            json_text = "".join(model_data.split())
            data = json.loads(json_text)
            model_data = json.dumps(data, separators=(",", ":"))

            for i in range(10):
                action = await temporal_unit.run_action(
                    "create-authorization-model",
                    model=model_data,
                )
                result = await action.wait()
                logger.info(f"attempt {i} -> action result {result.status} {result.results}")
                if result.status == "completed" and result.results == {"return-code": 0}:
                    break
                time.sleep(2)

        await ops_test.model.wait_for_idle(
            apps=[SERVER_APP_NAME],
            status="active",
            raise_on_blocked=True,
            timeout=300,
        )

        assert ops_test.model.applications[APP_NAME].status == "active"

        await run_tctl_action(ops_test, namespace="integrations")
