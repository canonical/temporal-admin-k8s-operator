#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm definition and helpers."""

import functools
import json
import logging

from ops import main
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from state import State

logger = logging.getLogger(__name__)
WORKLOAD_VERSION = "1.23.1"


def log_event_handler(method):
    """Log when an event handler method is executed.

    Args:
        method: method wrapped by the decorator.

    Returns:
        Decorator wrapper.
    """

    @functools.wraps(method)
    def decorated(self, event):
        """Log decorator method.

        Args:
            event: The event triggered when the relation changes.

        Returns:
            Decorated method.
        """
        logger.debug(f"running {method.__name__}")
        try:
            return method(self, event)
        finally:
            logger.debug(f"completed {method.__name__}")

    return decorated


class TemporalAdminK8SCharm(CharmBase):
    """Temporal admin charm."""

    def __init__(self, *args):
        """Construct.

        Args:
            args: Ignore.
        """
        super().__init__(*args)
        self._state = State(self.app, lambda: self.model.get_relation("peer"))
        self.name = "temporal-admin"

        # Handle basic charm lifecycle.
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.temporal_admin_pebble_ready, self._on_temporal_admin_pebble_ready)

        # Handle admin:temporal relation.
        self.framework.observe(self.on.admin_relation_changed, self._on_admin_relation_changed)
        self.framework.observe(self.on.admin_relation_broken, self._on_admin_relation_broken)

        # Handle action
        self.framework.observe(self.on.tctl_action, self._on_tctl_action)
        self.framework.observe(self.on.setup_schema_action, self._on_setup_schema_action)

    @log_event_handler
    def _on_install(self, event):
        """Install temporal admin tools.

        Args:
            event: The event triggered when the relation changed.
        """
        self.unit.status = MaintenanceStatus("installing temporal admin tools")

    @log_event_handler
    def _on_temporal_admin_pebble_ready(self, event):
        """Handle workload being ready.

        Args:
            event: The event triggered when the relation changed.
        """
        # Auto schema set up is only need once initially.
        if self._state.is_initial_schema_ready:
            self.unit.status = ActiveStatus()
            return

        try:
            self._setup_db_schemas(event)
        except Exception:
            self.unit.status = BlockedStatus("error setting up schema. remove relation and try again.")

    @log_event_handler
    def _on_admin_relation_changed(self, event):
        """Handle changes on the admin:temporal relation.

        Get reported database connection info. Then use that info to set up the
        schema. Then report back that the schema is ready.

        Args:
            event: The event triggered when the relation changed.
        """
        if not self._state.is_ready():
            event.defer()
            return

        self.unit.status = WaitingStatus(f"handling {event.relation.name} change")
        database_connections = event.relation.data[event.app].get("database_connections")
        self._state.database_connections = json.loads(database_connections) if database_connections else None
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_admin_relation_broken(self, event):
        """Handle the admin:temporal relation being broken.

        Args:
            event: The event triggered when the relation was broken.
        """
        if not self._state.is_ready():
            event.defer()
            return

        self._state.database_connections = None
        self._state.is_initial_schema_ready = False
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_tctl_action(self, event):
        """Run the tctl command line tool.

        Args:
            event: The event triggered when the action is triggered.
        """
        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.fail("cannot connect to container")
            return

        server_name = self.model.config["server-name"] or "temporal-k8s"
        args = ["--address", f"{server_name}:7236", *event.params["args"].split()]
        try:
            output = execute(container, "tctl", *args)
        except Exception as err:
            event.fail(f"command failed: {err}")
            return

        event.set_results({"result": "command succeeded", "output": output})

    @log_event_handler
    def _on_setup_schema_action(self, event):
        """Set up the database schemas.

        Args:
            event: The event triggered when the action is triggered.
        """
        try:
            self._setup_db_schemas(event)
        except Exception as err:
            event.fail(err)

    # flake8: noqa: C901
    def _setup_db_schemas(self, event):
        """Initialize the db schemas if db connections info is available.

        Args:
            event: The event triggered when the relation changed.

        Raises:
            Exception: if the schemas were not set up successfully.
        """
        if not self.model.unit.is_leader():
            return

        if not self._state.is_ready():
            event.defer()
            return

        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.defer()
            return

        if not self._state.database_connections:
            self.unit.status = BlockedStatus("admin:temporal relation: database connections info not available")
            return

        schema_dirs = {
            "db": "/etc/temporal/schema/postgresql/v12/temporal/versioned",
            "visibility": "/etc/temporal/schema/postgresql/v12/visibility/versioned",
        }
        for key, database_connection in self._state.database_connections.items():
            logger.info(f"initializing {key} schema")
            try:
                command_args = [
                    "--plugin",
                    "postgres",
                    "--endpoint",
                    database_connection["host"],
                    "--port",
                    database_connection["port"],
                    "--database",
                    database_connection["dbname"],
                    "--user",
                    database_connection["user"],
                    "--password",
                    database_connection["password"],
                    "setup-schema",
                    "-v",
                    "0.0",
                ]

                if database_connection.get("tls", False):
                    command_args.insert(2, "--tls")
                    command_args.insert(3, "--tls-disable-host-verification")

                execute(container, "temporal-sql-tool", *command_args)

                command_args = [
                    "--plugin",
                    "postgres",
                    "--endpoint",
                    database_connection["host"],
                    "--port",
                    database_connection["port"],
                    "--database",
                    database_connection["dbname"],
                    "--user",
                    database_connection["user"],
                    "--password",
                    database_connection["password"],
                    "update-schema",
                    "-d",
                    schema_dirs[key],
                ]

                # Conditionally add the TLS flags
                if database_connection.get("tls", False):
                    command_args.insert(2, "--tls")
                    command_args.insert(3, "--tls-disable-host-verification")

                execute(container, "temporal-sql-tool", *command_args)
            except Exception as e:
                logger.error(f"Error setting up schema: {e}")
                raise Exception from e

        admin_relations = self.model.relations["admin"]
        if not admin_relations:
            # Can this happen? Probably in a race between hook execution and
            # removed relation?
            logger.debug("admin:temporal: not notifying schema readiness: admin relation not available")
            self.unit.status = BlockedStatus("admin:temporal relation: not available")
            return
        logger.info("notifying schemas are ready")
        for relation in admin_relations:
            logger.debug(f"admin:temporal: notifying schema readiness on relation {relation.id}")
            relation.data[self.app].update({"schema_status": "ready"})

        self._state.is_initial_schema_ready = True
        self.unit.set_workload_version(WORKLOAD_VERSION)
        self.unit.status = ActiveStatus()


def execute(container, command, *args):
    """Execute the given command in the given container.

    Log the output and any warnings.

    Args:
        container: Container to execute command in.
        command: Command to be executed.
        args: Additional arguments needed for command execution.

    Returns:
        Output from executing the command.
    """
    cmd = [command] + list(args)
    proc = container.exec(cmd, timeout=60)
    output, warnings = proc.wait_output()
    for line in output.splitlines():
        logger.debug(f"{command}: {line.strip()}")
    if warnings:
        for line in warnings.splitlines():
            logger.warning(f"{command}: {line.strip()}")
    return output


if __name__ == "__main__":
    main.main(TemporalAdminK8SCharm)
