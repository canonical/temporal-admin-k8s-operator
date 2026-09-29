# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

# More extensive integration tests for this charm are at
# <https://github.com/canonical/temporal-k8s-operator/blob/main/tests/integration/test_charm.py>.


"""Temporal admin charm integration tests."""

import json
import logging
import time

import jubilant
import pytest
from conftest import POSTGRESQL_CHANNEL, TEMPORAL_CHANNEL
from helpers import APP_NAME, SERVER_APP_NAME, run_cli_action, run_setup_schema_action
from jubilant import TaskError

logger = logging.getLogger(__name__)


@pytest.fixture(name="deploy", scope="module")
def deploy(juju: jubilant.Juju, charm_path, charm_resources):
    """The app is up and running."""
    juju.model_config(values={"update-status-hook-interval": "1m"})

    # Deploy temporal server, temporal admin and postgresql charms
    juju.deploy(
        "postgresql-k8s",
        app="postgresql-k8s",
        channel=POSTGRESQL_CHANNEL,
        trust=True,
    )
    juju.deploy(
        SERVER_APP_NAME,
        app=SERVER_APP_NAME,
        channel=TEMPORAL_CHANNEL,
        config={"num-history-shards": 1},
    )
    juju.deploy(
        charm_path,
        app=APP_NAME,
        resources=charm_resources,
    )

    juju.wait(lambda status: jubilant.all_blocked(status, SERVER_APP_NAME, APP_NAME), timeout=600)
    juju.wait(lambda status: jubilant.all_active(status, "postgresql-k8s"), timeout=600)

    juju.integrate("temporal-k8s:db", "postgresql-k8s:database")
    juju.integrate("temporal-k8s:visibility", "postgresql-k8s:database")
    juju.integrate("temporal-k8s:admin", f"{APP_NAME}:admin")
    juju.integrate(f"{SERVER_APP_NAME}:temporal-host-info", f"{APP_NAME}:temporal-host-info")

    juju.wait(lambda status: jubilant.all_active(status, SERVER_APP_NAME), timeout=600)

    unit = juju.status().apps[APP_NAME].units[f"{APP_NAME}/0"]
    assert unit.workload_status.current == "active"


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("deploy")
class TestDeployment:
    """Integration tests for Temporal admin charm."""

    def test_cli_action(self, juju: jubilant.Juju):
        """Is it possible to run cli command via the action."""
        run_cli_action(juju, namespace="default")

    def test_setup_schema_action(self, juju: jubilant.Juju):
        """Is it possible to run setup schema via the action."""
        run_setup_schema_action(juju)

    def test_openfga_relation(self, juju: jubilant.Juju):
        """Add OpenFGA relation and authorization model."""
        juju.config(SERVER_APP_NAME, {"auth-enabled": True})
        juju.deploy("openfga-k8s", app="openfga-k8s", channel="latest/edge")
        juju.wait(
            lambda status: jubilant.all_blocked(status, SERVER_APP_NAME, "openfga-k8s"),
            timeout=1200,
        )

        logger.info("adding openfga postgresql relation")
        juju.integrate("openfga-k8s:database", "postgresql-k8s:database")

        juju.wait(lambda status: jubilant.all_active(status, "openfga-k8s"), timeout=1200)

        logger.info("adding openfga relation")
        juju.integrate(SERVER_APP_NAME, "openfga-k8s")

        juju.wait(lambda status: jubilant.all_blocked(status, SERVER_APP_NAME), timeout=600)

        logger.info("running the create authorization model action")
        with open("./temporal_auth_model.json", "r", encoding="utf-8") as model_file:
            model_data = model_file.read()

            # Remove whitespace and newlines from JSON object
            json_text = "".join(model_data.split())
            data = json.loads(json_text)
            model_data = json.dumps(data, separators=(",", ":"))

            for i in range(10):
                try:
                    task = juju.run(
                        f"{SERVER_APP_NAME}/0",
                        "create-authorization-model",
                        {"model": model_data},
                    )
                    logger.info("attempt %s -> action result %s %s", i, task.status, task.results)
                    if task.status == "completed" and task.return_code == 0:
                        break
                except TaskError as exc:
                    logger.info(
                        "attempt %s -> action result %s %s",
                        i,
                        exc.task.status,
                        exc.task.results,
                    )
                time.sleep(2)

        juju.wait(lambda status: jubilant.all_active(status, SERVER_APP_NAME), timeout=300)

        assert juju.status().apps[APP_NAME].app_status.current == "active"

        run_cli_action(juju, namespace="integrations")

    def test_remove_server(self, juju: jubilant.Juju):
        """Admin charm goes to blocked state once relation with the server charm is removed."""
        juju.remove_application(SERVER_APP_NAME)
        juju.wait(lambda status: SERVER_APP_NAME not in status.apps, timeout=300)

        juju.wait(lambda status: jubilant.all_blocked(status, APP_NAME), timeout=300)

        unit = juju.status().apps[APP_NAME].units[f"{APP_NAME}/0"]
        assert unit.workload_status.current == "blocked"
