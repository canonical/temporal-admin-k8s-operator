#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
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
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from state import State

logger = logging.getLogger(__name__)
WORKLOAD_VERSION = "1.24.3"
SQL_TOOL = f"/bin/temporal-sql-tool-{WORKLOAD_VERSION}"
SCHEMA_ROOT = f"/etc/temporal/schema-{WORKLOAD_VERSION}/postgresql/v12"


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
        self.framework.observe(self.on.pre_upgrade_check_action, self._on_pre_upgrade_check_action)
        self.framework.observe(self.on.update_status, self._on_temporal_admin_pebble_ready)
        self.framework.observe(self.on.leader_elected, self._on_temporal_admin_pebble_ready)
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
            "before this refresh (run the pre-upgrade-check action to review observable prerequisites).",
            WORKLOAD_VERSION,
        )
        self._state.upgrade_schema_pending = True
        self._publish_schema_status("migrating")
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
        """Initialize schemas after Pebble starts, or publish existing readiness."""
        if not self.unit.is_leader():
            return
        if not self._state.is_ready():
            event.defer()
            return
        if self._state.schema_workload_version == WORKLOAD_VERSION and not self._state.upgrade_schema_pending:
            self._publish_ready_to_admin_relations()
            return
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_admin_relation_changed(self, event):
        """Migrate when credentials arrive and publish readiness on a new relation."""
        if not self.unit.is_leader():
            return
        if not self._state.is_ready():
            event.defer()
            return
        if not event.app or not event.relation.data[event.app].get("database_connections"):
            return
        if self._state.schema_workload_version == WORKLOAD_VERSION and not self._state.upgrade_schema_pending:
            self._publish_ready_to_admin_relations()
            return
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
        """Run migrations explicitly with all available admin credentials."""
        if not self.unit.is_leader():
            event.fail("schema migration must run on the leader")
            return
        if not self._state.is_ready():
            event.fail("peer relation unavailable")
            return
        if not self._setup_db_schemas(event):
            event.fail("schema migration incomplete; inspect charm logs")

    @log_event_handler
    def _on_pre_upgrade_check_action(self, event):
        """Validate upgrade prerequisites observable by this charm.

        Reports the target schema version this charm would migrate to, the
        version schemas were last migrated to, and whether at least one
        admin relation currently reports database connectivity. This action
        does NOT verify that a database backup exists or is restorable;
        operators must create and verify a full PostgreSQL backup via the
        postgresql-k8s charm's backup actions before proceeding.

        Args:
            event: The action event.
        """
        relations = self.model.relations.get("admin", [])
        connected_ids = sorted(
            relation.id
            for relation in relations
            if relation.app and relation.data[relation.app].get("database_connections")
        )
        current_version = self._state.schema_workload_version or "unset"
        results = {
            "target-schema-version": WORKLOAD_VERSION,
            "current-schema-version": current_version,
            "schema-ready-for-target": str(
                current_version == WORKLOAD_VERSION and not self._state.upgrade_schema_pending
            ).lower(),
            "database-connectivity": "ok" if connected_ids else "no admin relation reports database connectivity",
            "admin-relations-checked": ",".join(str(i) for i in connected_ids) or "none",
            "backup-verified": "false",
            "warning": (
                "This action does not verify that a database backup exists or is restorable. "
                "Create and verify a full PostgreSQL backup via 'juju run postgresql-k8s/leader "
                "create-backup=full' before proceeding with the upgrade."
            ),
        }
        event.set_results(results)
        if not connected_ids:
            event.fail("no admin relation reports database connectivity; cannot assess migration readiness")

    # flake8: noqa: C901
    def _setup_db_schemas(self, event):
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
        if not self.model.unit.is_leader() or not self._state.is_ready():
            if not isinstance(event, ActionEvent):
                event.defer()
            return False

        self._state.upgrade_schema_pending = True
        self._publish_schema_status("migrating")
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

                    if not self._state.is_initial_schema_ready:
                        execute(container, SQL_TOOL, *args, "setup-schema", "-v", "0.0")
                    execute(container, SQL_TOOL, *args, "update-schema", "-d", schema_dirs[key])
                except Exception as exc:
                    logger.warning(
                        "Schema %s migration failed using admin relation %s (%s)",
                        key,
                        relation.id,
                        type(exc).__name__,
                    )
                    continue
                logger.info("Schema %s migrated using admin relation %s", key, relation.id)
                pending.remove(key)
            if not pending:
                break

        if pending:
            self._publish_schema_status("failed")
            self.unit.status = BlockedStatus("schema migration incomplete for: " + ", ".join(sorted(pending)))
            return False
        self._state.schema_workload_version = WORKLOAD_VERSION
        self._state.is_initial_schema_ready = True
        self._state.upgrade_schema_pending = False
        self._publish_ready_to_admin_relations()
        self.unit.set_workload_version(WORKLOAD_VERSION)
        self.unit.status = ActiveStatus()
        return True

    def _publish_ready_to_admin_relations(self):
        """Tell new and existing admin relations that the initialized schema is ready."""
        self._publish_schema_status("ready")
        self.unit.set_workload_version(WORKLOAD_VERSION)
        self.unit.status = ActiveStatus()

    def _publish_schema_status(self, status):
        """Only advertise a workload version after both migrations succeed."""
        for relation in self.model.relations.get("admin", []):
            data = relation.data[self.app]
            data["schema_status"] = status
            if status == "ready":
                data["schema_version"] = WORKLOAD_VERSION
            else:
                data.pop("schema_version", None)


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
