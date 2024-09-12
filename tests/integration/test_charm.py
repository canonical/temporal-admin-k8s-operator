# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

# More extensive integration tests for this charm are at
# <https://github.com/canonical/temporal-k8s-operator/blob/main/tests/integration/test_charm.py>.


"""Temporal admin charm integration tests."""

import json
import logging
import time
from pathlib import Path

import pytest
import pytest_asyncio
from helpers import (
    APP_NAME,
    METADATA,
    SERVER_APP_NAME,
    run_setup_schema_action,
    run_tctl_action,
)
from pytest import FixtureRequest
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture(scope="module", name="charm")
async def charm_fixture(request: FixtureRequest, ops_test: OpsTest) -> str | Path:
    """Fetch the path to charm."""
    charms = request.config.getoption("--charm-file")
    if not charms:
        charm = await ops_test.build_charm(".")
        assert charm, "Charm not built"
        return charm
    return charms[0]


@pytest_asyncio.fixture(name="deploy", scope="module")
async def deploy(ops_test: OpsTest, charm: str):
    """The app is up and running."""
    await ops_test.model.set_config({"update-status-hook-interval": "1m"})
    resources = {"temporal-admin-image": METADATA["resources"]["temporal-admin-image"]["upstream-source"]}

    # Deploy temporal server, temporal admin and postgresql charms
    await ops_test.model.deploy(SERVER_APP_NAME, channel="edge", config={"num-history-shards": 1})
    await ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME)
    await ops_test.model.deploy("postgresql-k8s", channel="14/stable", trust=True)
    await ops_test.model.deploy("self-signed-certificates", channel="latest/stable")

    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            apps=[SERVER_APP_NAME, APP_NAME], status="blocked", raise_on_blocked=False, timeout=600
        )
        await ops_test.model.wait_for_idle(
            apps=["postgresql-k8s", "self-signed-certificates"], status="active", raise_on_blocked=False, timeout=600
        )

        await ops_test.model.integrate("self-signed-certificates", "postgresql-k8s")
        await ops_test.model.integrate(f"{APP_NAME}:db", "postgresql-k8s:database")
        await ops_test.model.integrate(f"{APP_NAME}:visibility", "postgresql-k8s:database")
        await ops_test.model.wait_for_idle(
            apps=["self-signed-certificates", "postgresql-k8s"],
            status="active",
            raise_on_blocked=False,
            timeout=300,
        )
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status="blocked", raise_on_blocked=False, timeout=300)

        await ops_test.model.integrate("temporal-k8s:db", "postgresql-k8s:database")
        await ops_test.model.integrate("temporal-k8s:visibility", "postgresql-k8s:database")
        await ops_test.model.integrate("temporal-k8s:admin", f"{APP_NAME}:admin")

        await ops_test.model.wait_for_idle(
            apps=[APP_NAME, SERVER_APP_NAME], status="active", raise_on_blocked=False, timeout=300
        )

        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("deploy")
class TestDeployment:
    """Integration tests for Temporal admin charm."""

    async def test_tctl_action(self, ops_test: OpsTest):
        """Is it possible to run tctl command via the action."""
        await run_tctl_action(ops_test, namespace="default")

    async def test_setup_schema_action(self, ops_test: OpsTest):
        """Is it possible to run setup schema via the action."""
        await run_setup_schema_action(ops_test)

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
            status="active",
            raise_on_blocked=False,
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

    async def test_remove_server(self, ops_test: OpsTest):
        """Admin charm goes to blocked state once relation with the server charm is removed."""
        await ops_test.model.applications[SERVER_APP_NAME].destroy()
        await ops_test.model.block_until(lambda: SERVER_APP_NAME not in ops_test.model.applications)

        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="blocked",
            raise_on_blocked=False,
            timeout=300,
        )

        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "blocked"
