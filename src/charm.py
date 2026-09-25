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
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from ops.pebble import ExecError
from state import State

logger = logging.getLogger(__name__)
WORKLOAD_VERSION = "1.24.3"
SQL_TOOL = f"/bin/temporal-sql-tool-{WORKLOAD_VERSION}"
SCHEMA_ROOT = f"/etc/temporal/schema-{WORKLOAD_VERSION}/postgresql/v12"
SCHEMA_VERSIONS = {"db": "1.12", "visibility": "1.6"}


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

        self._reconcile_schemas(event)

    @log_event_handler
    def _on_upgrade_charm(self, event):
        """Reconcile schemas after refresh, invalidating old readiness first."""
        logger.warning(
            "A verified PostgreSQL backup is required before refresh; backup status is not checked by this charm"
        )
        if self.unit.is_leader():
            self._publish_schema_status("migrating")
            if self._state.is_ready():
                self._state.schema_workload_version = None
        self._reconcile_schemas(event)

    def _load_database_connections(self, excluded_relation_id=None):
        """Select and pin an authorized existing relation for each database."""
        candidates = {}
        for relation in sorted(self.model.relations.get("admin", []), key=lambda r: r.id):
            if relation.app and relation.id != excluded_relation_id:
                raw = relation.data[relation.app].get("database_connections")
                if raw:
                    candidates[str(relation.id)] = json.loads(raw)

        if not candidates:
            raise ValueError("admin:temporal relation: database connections info not available")

        pinned = self._state.migration_relations or {}
        selected, sources = {}, {}

        for key in SCHEMA_VERSIONS:
            endpoints = {
                (c[key]["host"], str(c[key]["port"]), c[key]["dbname"]) for c in candidates.values() if c.get(key)
            }
            if len(endpoints) != 1:
                raise ValueError(f"{key}: admin relations must refer to the same database endpoint")

            for relation_id, connections in candidates.items():
                if key in pinned and relation_id != pinned[key]:
                    continue
                if not connections.get(key):
                    continue
                selected[key], sources[key] = connections[key], relation_id
                break

            if key not in selected:
                raise ValueError(f"{key}: no authorized migration identity; check pinned admin relation")

        return selected, sources

    def _reconcile_schemas(self, event, excluded_relation_id=None):
        """Keep failed migrations blocked and retryable without removing relations."""
        try:
            self._setup_db_schemas(event, excluded_relation_id)
        except Exception as error:
            self._publish_schema_status("failed")
            self.unit.status = BlockedStatus(f"{error}; run pre-upgrade-check, then setup-schema")
            logger.error("Schema migration failed: %s", error)

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

        self._reconcile_schemas(event)

    @log_event_handler
    def _on_admin_relation_broken(self, event):
        """Handle the admin:temporal relation being broken.

        Args:
            event: The event triggered when the relation was broken.
        """
        if not self._state.is_ready():
            event.defer()
            return

        self._reconcile_schemas(event, excluded_relation_id=event.relation.id)

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
            self._setup_db_schemas(event)
        except Exception as err:
            self._publish_schema_status("failed")
            self.unit.status = BlockedStatus(str(err))
            event.fail(str(err))

    def _on_pre_upgrade_check_action(self, event):
        """Check migration readiness configuration."""
        if not self._state.is_ready():
            event.fail("peer relation not ready")
            return
        try:
            connections, sources = self._load_database_connections()
        except Exception as error:
            event.fail(str(error))
            return

        event.set_results(
            {
                "target-version": WORKLOAD_VERSION,
                "schemas": "Dynamic validation handled by sql-tool during setup-schema",
                "migration-users": json.dumps({key: c["user"] for key, c in connections.items()}),
                "migration-relations": json.dumps(sources),
                "backup": "NOT VERIFIED: create and restore-test a PostgreSQL charm backup before refresh",
            }
        )

    # flake8: noqa: C901
    def _setup_db_schemas(self, event, excluded_relation_id=None):
        if not self.model.unit.is_leader() or not self._state.is_ready():
            event.defer()
            return

        self._publish_schema_status("migrating")
        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.defer()
            return

        # Versions removed from return unpack
        connections, sources = self._load_database_connections(excluded_relation_id)

        self._state.migration_relations = sources
        self._state.database_connections = connections
        self.unit.status = MaintenanceStatus("updating Temporal database schemas")

        schema_dirs = {
            "db": f"{SCHEMA_ROOT}/temporal/versioned",
            "visibility": f"{SCHEMA_ROOT}/visibility/versioned",
        }

        for key, database_connection in connections.items():
            logger.info("Migrating %s using relation %s user %s", key, sources[key], database_connection["user"])

            command_args = [
                "--plugin", "postgres12",
                "--endpoint", database_connection["host"],
                "--port", str(database_connection["port"]),
                "--database", database_connection["dbname"],
                "--user", database_connection["user"],
            ]

            if database_connection.get("tls", False):
                command_args.extend(["--tls", "--tls-disable-host-verification"])

            environment = {"SQL_PASSWORD": database_connection["password"]}

            try:
                # Optimistically attempt to update the schema
                execute(
                    container, SQL_TOOL, *command_args, "update-schema", "-d", schema_dirs[key],
                    environment=environment, timeout=1800
                )
            except ExecError as error:
                err_out = str(error.stderr or error.stdout).lower()

                # Check if error is due to a brand-new database missing the schema table
                if "relation \"schema_version\" does not exist" in err_out or "not found" in err_out:
                    logger.info(f"{key}: schema_version table missing. Initializing schema first.")
                    try:
                        execute(
                            container, SQL_TOOL, *command_args, "setup-schema", "-v", "0.0",
                            environment=environment, timeout=1800
                        )
                        # Retry the update
                        execute(
                            container, SQL_TOOL, *command_args, "update-schema", "-d", schema_dirs[key],
                            environment=environment, timeout=1800
                        )
                    except ExecError as setup_error:
                        setup_err_msg = str(setup_error.stderr or setup_error.stdout)
                        logger.error(f"{key} setup-schema failed: {setup_err_msg}")
                        raise ValueError(f"Failed to initialize {key} schema. Check logs.") from setup_error

                # Check if error is due to insufficient privileges (graceful failure)
                elif "permission denied" in err_out or "privilege" in err_out:
                    logger.error(f"Permission denied for {key}: {err_out}")
                    raise ValueError(f"{key} migration failed: Insufficient database privileges.") from error

                else:
                    logger.error(f"{key} schema tool failed: {err_out}")
                    raise ValueError(f"{key} migration failed. See logs for details.") from error

        admin_relations = self.model.relations.get("admin")
        if not admin_relations:
            self.unit.status = BlockedStatus("admin:temporal relation: not available")
            return

        self._state.is_initial_schema_ready = True
        self._state.schema_workload_version = WORKLOAD_VERSION
        self._publish_schema_status("ready", WORKLOAD_VERSION)
        self.unit.set_workload_version(WORKLOAD_VERSION)
        self.unit.status = ActiveStatus()

    def _publish_schema_status(self, status, version=None):
        """Publish migration progress and the version proven ready to the server."""
        if not self.unit.is_leader():
            return
        for relation in self.model.relations.get("admin", []):
            data = {"schema_status": status}
            if version is not None:
                data["schema_version"] = version
            else:
                # A previous charm revision may have left a stale ready version.
                relation.data[self.app].pop("schema_version", None)
            relation.data[self.app].update(data)


def execute(container, command, *args, environment=None, timeout=60):
    cmd = [command] + list(args)
    proc = container.exec(cmd, timeout=timeout, environment=environment)
    # wait_output() automatically raises ops.pebble.ExecError if exit code != 0
    output, warnings = proc.wait_output()

    for line in output.splitlines():
        logger.debug(f"{command}: {line.strip()}")
    if warnings:
        for line in warnings.splitlines():
            logger.warning(f"{command}: {line.strip()}")

    return output


if __name__ == "__main__":
    main.main(TemporalAdminK8SCharm)
