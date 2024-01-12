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


async def run_tctl_action(ops_test: OpsTest, namespace):
    """Run tctl action from the admin charm to create a namespace.

    Args:
        ops_test: PyTest object.
        namespace: Namespace to create in Temporal server.
    """
    action = (
        await ops_test.model.applications[APP_NAME]
        .units[0]
        .run_action("tctl", args=f"--ns {namespace} namespace register -rd 3")
    )
    result = (await action.wait()).results

    logger.info(f"tctl result: {result}")

    await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", raise_on_blocked=False, timeout=600)

    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"
    assert ("output" in result) and f"Namespace {namespace} successfully registered" in result["output"]
