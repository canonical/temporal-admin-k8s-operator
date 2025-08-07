# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Temporal admin charm integration test helpers."""

import logging
from pathlib import Path

import yaml
from pytest_operator.plugin import OpsTest

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
SERVER_APP_NAME = "temporal-k8s"

logger = logging.getLogger(__name__)


async def run_cli_action(ops_test: OpsTest, namespace):
    """Run cli action from the admin charm to create a namespace.

    Args:
        ops_test: PyTest object.
        namespace: Namespace to create in Temporal server.
    """
    action = (
        await ops_test.model.applications[APP_NAME]
        .units[0]
        .run_action("cli", args=f"operator namespace --namespace {namespace} create")
    )
    action_output = await action.wait()

    logger.info(f"cli result: {action_output.results}")

    await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", raise_on_blocked=False, timeout=600)

    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"
    assert (
        "output" in action_output.results
    ) and f"Namespace {namespace} successfully registered" in action_output.results["output"]


async def run_setup_schema_action(ops_test: OpsTest):
    """Run setup schema action from the admin charm.

    Args:
        ops_test: PyTest object.
    """
    action = await ops_test.model.applications[APP_NAME].units[0].run_action("setup-schema")
    result = (await action.wait()).results

    logger.info(f"schema setup result: {result}")

    await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", raise_on_blocked=False, timeout=600)

    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"
