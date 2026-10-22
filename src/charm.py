#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm definition and helpers."""

import functools
import json
import logging

from charms.certificate_transfer_interface.v1.certificate_transfer import (
    CertificateTransferRequires,
)
from charms.temporal_k8s.v0.temporal_host_info import TemporalHostInfoRequirer
from ops import main
from ops.charm import (
    ActionEvent,
    CharmBase,
    InstallEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    UpgradeCharmEvent,
)
from ops.framework import StoredState
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import ExecError

logger = logging.getLogger(__name__)
RECEIVE_CA_CERT_RELATION_NAME = "receive-ca-cert"
CA_CERT_PATH = "/etc/temporal/certs/frontend-ca.pem"
WORKLOAD_VERSION = "1.31.2"
SQL_TOOL = "temporal-sql-tool"
SCHEMA_ROOT = "/etc/temporal/schema/postgresql/v12"
# update-schema can take a long time on a large, already-populated DB and its duration can't be predicted;
# the 60s default on execute() is far too tight, so allow 45 minutes.
SCHEMA_MIGRATION_TIMEOUT = 45 * 60
# SQL tool stderr when the user lacks rights, e.g. "pq: must be owner of table executions_visibility".
PERMISSION_ERRORS = ("pq: permission denied", "pq: must be owner")
# SQL tool stderr when the database cannot be reached at all (not for bad credentials), e.g.
# 'Unable to connect to SQL database. {"error": "dial tcp ...: connect: connection refused"}'.
UNREACHABLE_DB_ERRORS = ("Unable to connect to SQL database", "dial tcp")
# Events that always re-run the (idempotent) migration, even when the published state is already current.
FORCING_EVENTS = (UpgradeCharmEvent, RelationChangedEvent, ActionEvent)


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


class ExitWithStatusError(Exception):
    """Exception raised to exit reconcile with a specific unit status."""

    def __init__(self, msg: str, status_type):
        """Construct.

        Args:
            msg: The status message.
            status_type: The ops status class to report.
        """
        super().__init__(str(msg))
        self.msg = str(msg)
        self.status_type = status_type

    @property
    def status(self):
        """Return the Juju unit status represented by this exception."""
        return self.status_type(self.msg)


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

    # Unit-local, never published to a relation: only records that a forced migration has not yet succeeded.
    _stored = StoredState()

    def __init__(self, *args):
        """Construct.

        Args:
            args: Ignore.
        """
        super().__init__(*args)
        self.name = "temporal-admin"
        self._stored.set_default(forced_migration_pending=False)

        for event in [
            self.on.install,
            self.on.upgrade_charm,
            self.on.config_changed,
            self.on.leader_elected,
            self.on.update_status,
            self.on.temporal_admin_pebble_ready,
            self.on.admin_relation_changed,
            self.on.admin_relation_broken,
        ]:
            self.framework.observe(event, self._reconcile)

        # Handle action
        self.framework.observe(self.on.cli_action, self._on_cli_action)
        self.framework.observe(self.on.setup_schema_action, self._on_setup_schema_action)
        # Handle temporal-host-info relation.
        self.host_info = TemporalHostInfoRequirer(self)

        # The CA that issued the frontend certificate, used to verify the
        # frontend when it serves gRPC over TLS. It comes straight from the
        # certificates provider, not from the Temporal server charm. It is read
        # when an action runs, so no events need observing here.
        self.ca_transfer = CertificateTransferRequires(self, RECEIVE_CA_CERT_RELATION_NAME)

    @property
    def _ca_bundle(self) -> str:
        """Return the CA bundle received over certificate_transfer, or an empty string."""
        return "\n".join(sorted(cert.strip() for cert in self.ca_transfer.get_all_certificates()))

    @log_event_handler
    def _reconcile(self, event):
        """Reconcile the charm towards the desired state; idempotent for every event.

        Args:
            event: The event that triggered the reconciliation.
        """
        try:
            if isinstance(event, InstallEvent):
                raise ExitWithStatusError("installing temporal admin tools", MaintenanceStatus)
            if self.unit.is_leader():
                self._reconcile_schemas(event)
        except ExitWithStatusError as e:
            self.unit.status = e.status
            return
        self.unit.status = ActiveStatus()

    def _reconcile_schemas(self, event):
        """Migrate the schemas when required and publish their readiness; leader only.

        Args:
            event: The event being reconciled.
        """
        if isinstance(event, RelationBrokenEvent):
            self._check_admin_relations()
        if isinstance(event, FORCING_EVENTS):
            # Recorded before any precondition can bail out, and cleared only once the migration succeeded, so a
            # forced migration that is cut short (container not ready, database down, ...) is retried on later events.
            self._stored.forced_migration_pending = True
        self._check_container_can_connect()
        relations = self._check_admin_relations()
        if not self._migration_required(event, relations):
            return
        if isinstance(event, UpgradeCharmEvent):
            logger.warning(
                "Starting schema migration to %s. This charm cannot verify that a database backup exists or "
                "is restorable; operators must create and verify a full PostgreSQL backup via postgresql-k8s "
                "before this refresh.",
                WORKLOAD_VERSION,
            )
        if isinstance(event, RelationChangedEvent):
            # Deliberate exception to "status only at the end": the migration can take up to 45 minutes, so show
            # progress meanwhile. Only the admin relation-changed handler did this originally.
            self.unit.status = WaitingStatus(f"handling {event.relation.name} change")
        self._setup_db_schemas(relations)
        self._publish_schema_ready(relations)
        self._stored.forced_migration_pending = False

    def _check_container_can_connect(self):
        """Verify the workload container is reachable.

        Raises:
            ExitWithStatusError: If the container cannot be connected to.
        """
        if not self.unit.get_container(self.name).can_connect():
            raise ExitWithStatusError("waiting for schema migration container", MaintenanceStatus)

    def _check_admin_relations(self):
        """Return the admin relations in ascending id order.

        Returns:
            The admin relations.

        Raises:
            ExitWithStatusError: If there is no admin relation.
        """
        relations = sorted(self.model.relations.get("admin", []), key=lambda relation: relation.id)
        if not relations:
            raise ExitWithStatusError("admin:temporal relation: not available", BlockedStatus)
        return relations

    def _schema_published_ready(self, relation) -> bool:
        """Report whether the schema was published as migrated to the current workload version.

        Args:
            relation: An admin relation.

        Returns:
            True if schema readiness and the current workload version are published on the relation.
        """
        data = relation.data[self.app]
        return data.get("schema_status") == "ready" and data.get("migrated_workload_version") == WORKLOAD_VERSION

    def _migration_required(self, event, relations) -> bool:
        """Decide whether the schemas must be migrated for this event.

        Args:
            event: The event being reconciled.
            relations: The admin relations.

        Returns:
            True when the event forces a migration, a forced migration is still pending, or a published schema state
            is not current.
        """
        if isinstance(event, FORCING_EVENTS) or self._stored.forced_migration_pending:
            return True
        return not all(self._schema_published_ready(relation) for relation in relations)

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

        # Relation data is the only source.
        if self.host_info.host and self.host_info.port:
            server_name = self.host_info.host
            server_port = self.host_info.port
        else:
            event.fail("temporal-host-info relation not established")
            return
        # Client flags go before the user's subcommand, so each must be a single
        # `--flag=value` token: the CLI only knows them on its subcommands, and
        # while looking for the subcommand it treats an unknown flag as taking
        # the next token as its value. `--tls --tls-ca-path <path>` would then
        # leave `<path>` looking like the subcommand.
        args = [f"--address={server_name}:{server_port}"]

        if self.host_info.tls:
            # The frontend serves gRPC over TLS: dial it over TLS and verify it
            # against the CA from the certificates provider. The CA is pushed on
            # every run so a rotated CA is always picked up.
            ca_bundle = self._ca_bundle
            if not ca_bundle:
                event.fail(f"Temporal frontend uses TLS: integrate {RECEIVE_CA_CERT_RELATION_NAME} with its CA")
                return
            container.push(CA_CERT_PATH, ca_bundle, make_dirs=True)
            args.extend(["--tls=true", f"--tls-ca-path={CA_CERT_PATH}"])

        args.extend(event.params["args"].split())
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
            if not self.unit.is_leader():
                event.fail("schema migration incomplete: setup-schema must be run on the leader unit")
                return
            self._reconcile(event)
            if self.unit.status != ActiveStatus():
                event.fail(f"schema migration incomplete: {self.unit.status.message}")
        except Exception as err:
            event.fail(err)

    # flake8: noqa: C901
    def _setup_db_schemas(self, relations):  # pylint: disable=too-many-branches,too-many-statements
        """Initialize and migrate the Temporal database schemas.

        Iterate through the admin relations and use their database connections to
        initialize and update the Temporal and visibility schemas. Each schema is
        attempted with available relation credentials until a migration succeeds.

        Args:
            relations: The admin relations, in the order they are tried.

        Raises:
            ExitWithStatusError: If the schemas could not all be migrated.
        """
        container = self.unit.get_container(self.name)
        schema_dirs = {
            "db": f"{SCHEMA_ROOT}/temporal/versioned",
            "visibility": f"{SCHEMA_ROOT}/visibility/versioned",
        }

        pending = set(schema_dirs)
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
                        raise ExitWithStatusError("waiting for the database to be reachable", WaitingStatus) from exc
                    # Only a permission error makes another relation's user worth trying.
                    if not any(e in (getattr(exc, "stderr", None) or "") for e in PERMISSION_ERRORS):
                        raise ExitWithStatusError(
                            f"schema {key} migration failed (not a permission error); "
                            "check the charm logs and then run the `setup-schema` action",
                            BlockedStatus,
                        ) from exc
                    continue
                logger.info("Schema %s migrated using database user %s", key, connection["user"])
                pending.remove(key)
            if not pending:
                break

        if pending:
            raise ExitWithStatusError(
                f"schema migration incomplete for: {', '.join(sorted(pending))}; no admin relation provides a "
                "database user that owns the Temporal tables",
                BlockedStatus,
            )

    def _publish_schema_ready(self, relations):
        """Tell admin relations the schema is ready; migrated_workload_version is not the SQL schema_version.

        Args:
            relations: The admin relations to publish to.
        """
        for relation in relations:
            relation.data[self.app].update({"schema_status": "ready", "migrated_workload_version": WORKLOAD_VERSION})
        self.unit.set_workload_version(WORKLOAD_VERSION)


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
