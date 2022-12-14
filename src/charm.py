#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm definition and helpers."""

import functools
import json
import logging

from ops import framework, main
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)


def log_event_handler(method):
    """Log when a event handler method is executed."""

    @functools.wraps(method)
    def decorated(self, event):
        logger.debug(f"running {method.__name__}")
        try:
            return method(self, event)
        finally:
            logger.debug(f"completed {method.__name__}")

    return decorated


class TemporalAdminK8SCharm(CharmBase):
    """Temporal admin charm."""

    _state = framework.StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.name = "temporal-admin"

        # Handle basic charm lifecycle.
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.temporal_admin_pebble_ready, self._on_temporal_admin_pebble_ready)
        self.framework.observe(self.on.tctl_action, self._on_tctl_action)

        # Handle admin:temporal relation.
        self._state.set_default(database_connections=None)
        self.framework.observe(self.on.admin_relation_changed, self._on_admin_relation_changed)

    @log_event_handler
    def _on_install(self, event):
        """Install temporal admin tools."""
        self.unit.status = MaintenanceStatus("installing temporal admin tools")

    @log_event_handler
    def _on_temporal_admin_pebble_ready(self, event):
        """Handle workload being ready."""
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_admin_relation_changed(self, event):
        """Handle changes on the admin:temporal relation.

        Get reported database connection info. Then use that info to set up the
        schema. Then report back that the schema is ready.
        """
        self.unit.status = WaitingStatus(f"handling {event.relation.name} change")
        database_connections = event.relation.data[event.app].get("database_connections")
        self._state.database_connections = json.loads(database_connections) if database_connections else None
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_tctl_action(self, event):
        """Run the tctl command line tool."""
        # TODO(frankban): make this work.
        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.fail("cannot connect to container")
            return

        args = event.params["args"].split()
        try:
            output = execute(container, "tctl", *args)
        except Exception as err:
            event.fail(f"command failed: {err}")
            return

        event.set_results({"result": "command succeeded", "output": output})

    def _setup_db_schemas(self, event):
        """Initialize the db schemas if db connections info is available."""
        if not self.model.unit.is_leader():
            return

        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.defer()
            return

        if not self._state.database_connections:
            self.unit.status = BlockedStatus("admin:temporal relation: database connections info not available")
            return

        schema_dirs = {
            "db": "/etc/temporal/schema/postgresql/v96/temporal/versioned",
            "visibility": "/etc/temporal/schema/postgresql/v96/visibility/versioned",
        }
        for key, database_connection in self._state.database_connections.items():
            logger.info(f"initializing {key} schema")
            execute(
                container,
                "temporal-sql-tool",
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
            )
            execute(
                container,
                "temporal-sql-tool",
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
            )

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

        self.unit.status = ActiveStatus()


def execute(container, command, *args):
    """Execute the given command in the given container.

    Log the output and any warnings.
    Return the output.
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
