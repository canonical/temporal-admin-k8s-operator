#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm definition and helpers."""

import functools
import json
import logging

from charms.temporal_k8s.v0.temporal_host_info import TemporalHostInfoRequirer
from ops import main
from ops.charm import ActionEvent, CharmBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import ExecError

from state import State

logger = logging.getLogger(__name__)
WORKLOAD_VERSION = "1.27.4"
SQL_TOOL = f"/bin/temporal-sql-tool-{WORKLOAD_VERSION}"
SCHEMA_ROOT = f"/etc/temporal/schema-{WORKLOAD_VERSION}/postgresql/v12"
# update-schema can take a long time on a large, already-populated DB and its duration can't be predicted;
# the 60s default on execute() is far too tight, so allow 45 minutes.
SCHEMA_MIGRATION_TIMEOUT = 45 * 60
# SQL tool stderr when the user lacks rights, e.g. "pq: must be owner of table executions_visibility".
PERMISSION_ERRORS = ("pq: permission denied", "pq: must be owner")
# SQL tool stderr when the database cannot be reached at all (not for bad credentials), e.g.
# 'Unable to connect to SQL database. {"error": "dial tcp ...: connect: connection refused"}'.
UNREACHABLE_DB_ERRORS = ("Unable to connect to SQL database", "dial tcp")


def _failure_detail(exc: Exception) -> str:
    """Describe a failed command without the command line, which contains credentials.

    Args:
        exc: The exception raised while running the SQL tool.

    Returns:
        Exit code and the tail of stderr for an ExecError, otherwise the exception type.
    """
    if isinstance(exc, ExecError):
        return f"exit code {exc.exit_code}: {(exc.stderr or '').strip()[-500:]}"
    return type(exc).__name__


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
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.temporal_admin_pebble_ready, self._on_temporal_admin_pebble_ready)

        # Handle admin:temporal relation.
        self.framework.observe(self.on.admin_relation_changed, self._on_admin_relation_changed)
        self.framework.observe(self.on.admin_relation_broken, self._on_admin_relation_broken)

        # Handle action
        self.framework.observe(self.on.cli_action, self._on_cli_action)
        self.framework.observe(self.on.setup_schema_action, self._on_setup_schema_action)
        # Handle temporal-host-info relation.
        self.host_info = TemporalHostInfoRequirer(self)

    @log_event_handler
    def _on_upgrade_charm(self, event):
        """Run schema migrations after the charm is upgraded.

        Mark schema migration as pending and attempt to update the Temporal
        database and visibility schemas using credentials from the available
        admin relations.

        Args:
            event: The upgrade-charm event.
        """
        if not self.unit.is_leader():
            return

        if not self._state.is_ready():
            event.defer()
            return

        logger.warning(
            "Starting schema migration to %s. This charm cannot verify that a database backup exists or "
            "is restorable; operators must create and verify a full PostgreSQL backup via postgresql-k8s "
            "before this refresh.",
            WORKLOAD_VERSION,
        )
        self._state.upgrade_schema_pending = True
        self._setup_db_schemas(event)

    @property
    def _deprecated_server_name(self) -> str | None:
        """Return configured fallback server name, if set."""
        raw = self.config.get("server-name")
        if raw is None:
            return None
        stripped = str(raw).strip()
        return stripped or None

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
            event: The event triggered when the workload container is ready.
        """
        if not self._state.is_ready():
            event.defer()
            return
        if self._state.is_initial_schema_ready and not self._state.upgrade_schema_pending:
            self.unit.status = ActiveStatus()
            return
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_admin_relation_changed(self, event):
        """Handle changes on the admin:temporal relation."""
        if not self._state.is_ready():
            event.defer()
            return
        self.unit.status = WaitingStatus(f"handling {event.relation.name} change")
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_admin_relation_broken(self, event):
        """Handle the admin:temporal relation being broken.

        Args:
            event: The event triggered when the relation was broken.
        """
        if not self.unit.is_leader():
            return
        if not self._state.is_ready():
            self.unit.status = BlockedStatus("peer relation unavailable")
            return
        if not self.model.relations.get("admin"):
            self._state.is_initial_schema_ready = False
            self.unit.status = BlockedStatus("admin:temporal relation: not available")

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
        # fallback to deprecated `server-name` only when explicitly configured.
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
        try:
            if not self._setup_db_schemas(event):
                reason = self.unit.status.message or "run it on the leader unit once the peer relation is ready"
                event.fail(f"schema migration incomplete: {reason}")
        except Exception as err:
            event.fail(err)

    # flake8: noqa: C901
    def _setup_db_schemas(self, event):  # pylint: disable=too-many-branches,too-many-statements,R0911
        """Initialize and migrate the Temporal database schemas.

        Iterate through the available admin relations and use their database
        connections to initialize and update the Temporal and visibility schemas.
        Each schema is attempted with available relation credentials until a
        migration succeeds.

        On successful migration of all schemas, mark the initial schema as ready,
        clear the pending upgrade state, publish schema readiness to admin
        relations, and set the unit to active.

        Args:
            event: The event that triggered the schema migration.

        Returns:
            True if all required schemas were migrated successfully, False if
            migration could not be completed.
        """
        if not self.model.unit.is_leader():
            return False

        if not self._state.is_ready():
            if not isinstance(event, ActionEvent):
                event.defer()
            return False

        container = self.unit.get_container(self.name)
        if not container.can_connect():
            self.unit.status = MaintenanceStatus("waiting for schema migration container")
            if not isinstance(event, ActionEvent):
                event.defer()
            return False

        schema_dirs = {
            "db": f"{SCHEMA_ROOT}/temporal/versioned",
            "visibility": f"{SCHEMA_ROOT}/visibility/versioned",
        }

        pending = set(schema_dirs)
        relations = sorted(self.model.relations.get("admin", []), key=lambda relation: relation.id)
        if not relations:
            self.unit.status = BlockedStatus("admin:temporal relation: not available")
            return False

        for relation in relations:
            if not relation.app:
                continue
            database_connections = relation.data[relation.app].get("database_connections")
            if not database_connections:
                continue
            try:
                connections = json.loads(database_connections)
            except (TypeError, ValueError):
                logger.warning("Invalid database_connections on admin relation %s", relation.id)
                continue
            if not isinstance(connections, dict):
                continue
            for key in sorted(pending):
                connection = connections.get(key)
                if not connection:
                    continue
                try:
                    args = [
                        "--plugin",
                        "postgres12",
                        "--endpoint",
                        connection["host"],
                        "--port",
                        str(connection["port"]),
                        "--database",
                        connection["dbname"],
                        "--user",
                        connection["user"],
                        "--password",
                        connection["password"],
                    ]
                    if connection.get("tls", False):
                        args[2:2] = ["--tls", "--tls-disable-host-verification"]

                    execute(container, SQL_TOOL, *args, "setup-schema", "-v", "0.0", timeout=SCHEMA_MIGRATION_TIMEOUT)
                    execute(
                        container,
                        SQL_TOOL,
                        *args,
                        "update-schema",
                        "-d",
                        schema_dirs[key],
                        timeout=SCHEMA_MIGRATION_TIMEOUT,
                    )
                except Exception as exc:
                    logger.warning(
                        "Schema %s migration failed using database user %s: %s",
                        key,
                        connection.get("user"),
                        _failure_detail(exc),
                    )
                    # Nothing ran against an unreachable database, so wait and retry on the next hook.
                    if all(e in (getattr(exc, "stderr", None) or "") for e in UNREACHABLE_DB_ERRORS):
                        self.unit.status = WaitingStatus("waiting for the database to be reachable")
                        if not isinstance(event, ActionEvent):
                            event.defer()
                        return False
                    # Only a permission error makes another relation's user worth trying.
                    if not any(e in (getattr(exc, "stderr", None) or "") for e in PERMISSION_ERRORS):
                        self.unit.status = BlockedStatus(
                            f"schema {key} migration failed (not a permission error); "
                            "check the charm logs and then run the `setup-schema` action"
                        )
                        return False
                    continue
                logger.info("Schema %s migrated using database user %s", key, connection["user"])
                pending.remove(key)
            if not pending:
                break

        if pending:
            self.unit.status = BlockedStatus(
                f"schema migration incomplete for: {', '.join(sorted(pending))}; no admin relation provides a "
                "database user that owns the Temporal tables"
            )
            return False
        self._state.is_initial_schema_ready = True
        self._state.upgrade_schema_pending = False
        self._notify_admin_relations_ready()
        return True

    def _notify_admin_relations_ready(self):
        """Tell admin relations the schema is ready; migrated_workload_version is not the SQL schema_version."""
        for relation in self.model.relations.get("admin", []):
            relation.data[self.app].update({"schema_status": "ready", "migrated_workload_version": WORKLOAD_VERSION})
        self.unit.set_workload_version(WORKLOAD_VERSION)
        self.unit.status = ActiveStatus()


def execute(container, command, *args, timeout=60):
    """Execute the given command in the given container.

    Log the output and any warnings.
    Args:
        container: Container to execute command in.
        command: Command to be executed.
        args: Additional arguments needed for command execution.
        timeout: Seconds to wait for the command to complete.

    Returns:
        Output from executing the command.
    """
    cmd = [command] + list(args)
    # Passing stdin avoids ops' stdin websocket writer, whose finalizer logs "Exception ignored" noise.
    proc = container.exec(cmd, timeout=timeout, stdin="")
    output, warnings = proc.wait_output()
    for line in output.splitlines():
        logger.debug(f"{command}: {line.strip()}")
    if warnings:
        for line in warnings.splitlines():
            logger.warning(f"{command}: {line.strip()}")
    return output


if __name__ == "__main__":
    main.main(TemporalAdminK8SCharm)
