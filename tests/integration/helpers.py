# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Temporal admin charm integration test helpers."""

import logging
from pathlib import Path

import jubilant
import yaml

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
SERVER_APP_NAME = "temporal-k8s"

logger = logging.getLogger(__name__)


def _unit_workload_status(juju: jubilant.Juju, app: str) -> str:
    """Return the workload status of unit 0 for an application.

    Args:
        juju: Jubilant Juju client.
        app: Application name.

    Returns:
        Workload status string for ``{app}/0``.
    """
    status = juju.status()
    return status.apps[app].units[f"{app}/0"].workload_status.current


def run_cli_action(juju: jubilant.Juju, namespace: str) -> None:
    """Run cli action from the admin charm to create a namespace.

    Args:
        juju: Jubilant Juju client.
        namespace: Namespace to create in Temporal server.
    """
    task = juju.run(
        f"{APP_NAME}/0",
        "cli",
        {"args": f"operator namespace --namespace {namespace} create"},
        wait=600,
    )
    logger.info("cli result: %s", task.results)

    juju.wait(lambda status: jubilant.all_active(status, APP_NAME), timeout=600)

    assert _unit_workload_status(juju, APP_NAME) == "active"
    assert "output" in task.results
    assert f"Namespace {namespace} successfully registered" in task.results["output"]


def run_setup_schema_action(juju: jubilant.Juju) -> None:
    """Run setup schema action from the admin charm.

    Args:
        juju: Jubilant Juju client.
    """
    task = juju.run(f"{APP_NAME}/0", "setup-schema", wait=600)
    logger.info("schema setup result: %s", task.results)

    juju.wait(lambda status: jubilant.all_active(status, APP_NAME), timeout=600)

    assert _unit_workload_status(juju, APP_NAME) == "active"
