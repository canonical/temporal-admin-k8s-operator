#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm definition and helpers."""

import functools
import json
import logging

import ops
from charms.temporal_k8s.v0.temporal_host_info import TemporalHostInfoRequirer
from ops import main
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus

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

        # Route all reconcilable events to _reconcile
        reconcile_events = [
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on["temporal-admin"].pebble_ready,
            self.on["peer"].relation_changed,
            self.on["admin"].relation_created,
            self.on["admin"].relation_joined,
            self.on["admin"].relation_changed,
            self.on["admin"].relation_departed,
            self.on["admin"].relation_broken,
        ]
        for event in reconcile_events:
            self.framework.observe(event, self._reconcile)

        # Dedicated handlers
        self.framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)
        self.framework.observe(self.on.cli_action, self._on_cli_action)
        self.framework.observe(self.on.setup_schema_action, self._on_setup_schema_action)

        # Handle temporal-host-info relation.
        self.host_info = TemporalHostInfoRequirer(self)

    @property
    def _deprecated_server_name(self) -> str | None:
        """Return configured fallback server name, if set."""
        raw = self.config.get("server-name")
        if raw is None:
            return None
        stripped = str(raw).strip()
        return stripped or None

    # -- Central Reconciliation Loop -----------------------------------

    @log_event_handler
    def _reconcile(self, event):
        """Central reconciliation loop for admin-only charm.

        Read inputs -> compute state -> write outputs.

        Args:
            event: The event that triggered reconciliation.
        """
        if not self._state.is_ready():
            return

        container = self.unit.get_container(self.name)
        if not container.can_connect():
            return

        # Phase 1: Read inputs -- read admin relation data (safe to poll)
        if self.unit.is_leader():
            self._read_admin_relation_data(event)

        # Phase 2/3: Run schema setup if db connections are available
        if not self._state.database_connections:
            return

        # Auto schema set up is only needed once initially.
        if self._state.is_initial_schema_ready:
            self.unit.set_workload_version(WORKLOAD_VERSION)
            return

        try:
            self._setup_db_schemas(container)
        except Exception:
            logger.exception("Error setting up schema")
            return

    def _read_admin_relation_data(self, event):
        """Read database connections from admin relation and persist to peer state.

        Safe to poll -- reads directly from relation databag.

        Args:
            event: The event that triggered the read.
        """
        # Handle relation-broken: clear state
        if isinstance(event, ops.RelationBrokenEvent) and event.relation.name == "admin":
            self._state.database_connections = None
            self._state.is_initial_schema_ready = False
            return

        admin_relations = self.model.relations["admin"]
        if not admin_relations:
            return

        for relation in admin_relations:
            database_connections = relation.data.get(relation.app, {}).get("database_connections")
            if database_connections:
                self._state.database_connections = json.loads(database_connections)
                return

    # -- Status Reporting ----------------------------------------------

    def _on_collect_unit_status(self, event):
        """Report unit status based on current state.

        Args:
            event: The collect-unit-status event.
        """
        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.add_status(WaitingStatus("Waiting for container"))
            return

        if not self._state.is_ready():
            event.add_status(WaitingStatus("Waiting for peer relation"))
            return

        if not self._state.database_connections:
            event.add_status(BlockedStatus("admin:temporal relation: database connections info not available"))
            return

        if not self._state.is_initial_schema_ready:
            event.add_status(BlockedStatus("error setting up schema. remove relation and try again."))
            return

        event.add_status(ActiveStatus())

    # -- Dedicated Handlers --------------------------------------------

    @log_event_handler
    def _on_cli_action(self, event):
        """Run the temporal command line tool.

        Args:
            event: The event triggered when the action is triggered.
        """
        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.fail("cannot connect to container")
            return

        # Relation data is authoritative when available. For upgrade compatibility,
        # fallback to deprecated server-name only when explicitly configured.
        if self.host_info.host and self.host_info.port:
            server_name = self.host_info.host
            server_port = self.host_info.port
        elif deprecated := self._deprecated_server_name:
            logger.warning(
                "The `server-name` config option is deprecated and will be removed in a future release; "
                "prefer the `temporal-host-info` relation."
            )
            server_name = deprecated
            server_port = 7236
        else:
            event.fail("temporal-host-info relation not established; set deprecated server-name config as fallback")
            return
        args = ["--address", f"{server_name}:{server_port}", *event.params["args"].split()]
        try:
            output = execute(container, "temporal", *args)
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
        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.fail("cannot connect to container")
            return
        try:
            self._setup_db_schemas(container)
        except Exception as err:
            event.fail(str(err))

    # -- Helpers -------------------------------------------------------

    # flake8: noqa: C901
    def _setup_db_schemas(self, container):
        """Initialize the db schemas if db connections info is available.

        Args:
            container: Container to execute commands in.

        Raises:
            Exception: if the schemas were not set up successfully.
        """
        if not self.model.unit.is_leader():
            return

        if not self._state.database_connections:
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
            logger.debug("admin:temporal: not notifying schema readiness: admin relation not available")
            return
        logger.info("notifying schemas are ready")
        for relation in admin_relations:
            logger.debug(f"admin:temporal: notifying schema readiness on relation {relation.id}")
            relation.data[self.app].update({"schema_status": "ready"})

        self._state.is_initial_schema_ready = True
        self.unit.set_workload_version(WORKLOAD_VERSION)


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
