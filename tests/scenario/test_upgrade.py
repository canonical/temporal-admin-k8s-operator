# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exercise the schema readiness contract during refresh and recovery."""

import dataclasses
import json
from unittest.mock import patch

import ops
import ops.testing
import pytest
from ops.pebble import ExecError

from charm import WORKLOAD_VERSION

# Workload version of the previous track, i.e. what admin published before this refresh.
PREVIOUS_WORKLOAD_VERSION = "1.29.7"


@pytest.fixture
def upgrade_state(peer_relation, admin_relation, temporal_admin_container):
    peer_relation.local_app_data["is_initial_schema_ready"] = "true"
    admin_relation.local_app_data.update(schema_status="ready", migrated_workload_version=PREVIOUS_WORKLOAD_VERSION)
    return ops.testing.State(
        leader=True, relations=[peer_relation, admin_relation], containers=[temporal_admin_container]
    )


def test_refresh_publishes_version_after_both_migrations(context, upgrade_state, admin_relation):
    with patch("charm.execute") as sql:
        result = context.run(context.on.upgrade_charm(), upgrade_state)
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }
    assert sql.call_count == 4
    assert sum("update-schema" in call.args for call in sql.call_args_list) == 2


@pytest.mark.parametrize("failed_call", range(4))
def test_failed_migration_keeps_old_version_and_resumes_on_pebble_ready(
    context, upgrade_state, admin_relation, temporal_admin_container, failed_call
):
    outcomes = [None] * 4
    outcomes[failed_call] = RuntimeError("migration interrupted")
    with patch("charm.execute", side_effect=outcomes):
        result = context.run(context.on.upgrade_charm(), upgrade_state)
    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": PREVIOUS_WORKLOAD_VERSION,
    }
    # update_status no longer retries migrations; pebble_ready resumes the pending upgrade instead.
    with patch("charm.execute") as sql:
        result = context.run(context.on.pebble_ready(temporal_admin_container), result)
    assert result.unit_status == ops.ActiveStatus()
    assert sql.call_count == 4
    assert result.get_relation(admin_relation.id).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION
    with patch("charm.execute") as sql:
        context.run(context.on.pebble_ready(temporal_admin_container), result)
    sql.assert_not_called()


def test_container_unavailable_defers_and_keeps_old_version(
    context, upgrade_state, admin_relation, temporal_admin_container
):
    state = dataclasses.replace(
        upgrade_state, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
    )
    result = context.run(context.on.upgrade_charm(), state)
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": PREVIOUS_WORKLOAD_VERSION,
    }
    assert result.deferred


def test_pebble_ready_resumes_pending_upgrade(context, peer_relation, admin_relation, temporal_admin_container):
    peer_relation.local_app_data["is_initial_schema_ready"] = "true"
    peer_relation.local_app_data["upgrade_schema_pending"] = "true"
    admin_relation.local_app_data.update(schema_status="ready", migrated_workload_version=PREVIOUS_WORKLOAD_VERSION)
    state = ops.testing.State(
        leader=True, relations=[peer_relation, admin_relation], containers=[temporal_admin_container]
    )
    with patch("charm.execute") as sql:
        result = context.run(context.on.pebble_ready(temporal_admin_container), state)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()


@pytest.mark.parametrize("leader", [True, False])
def test_pebble_ready_skips_migration_when_already_done(context, upgrade_state, temporal_admin_container, leader):
    with patch("charm.execute") as sql:
        result = context.run(
            context.on.pebble_ready(temporal_admin_container), dataclasses.replace(upgrade_state, leader=leader)
        )
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


def test_nonleader_does_not_migrate(context, upgrade_state):
    with patch("charm.execute") as sql:
        context.run(context.on.upgrade_charm(), dataclasses.replace(upgrade_state, leader=False))
    sql.assert_not_called()


def test_action_without_container_fails_without_deferring(context, upgrade_state, temporal_admin_container):
    state = dataclasses.replace(
        upgrade_state, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
    )
    with pytest.raises(ops.testing.ActionFailed):
        context.run(context.on.action("setup-schema"), state)


def _admin_relation(relation_id, user_suffix):
    """Build an admin relation whose db users are named after user_suffix.

    Args:
        relation_id: Relation id; relations are tried in ascending id order.
        user_suffix: Prefix of the database user published for both schemas.

    Returns:
        The admin relation, still advertising the previous workload version.
    """
    connections = {
        schema: {
            "dbname": f"temporal-k8s_{schema}",
            "host": "myhost",
            "password": "inner-light",  # nosec B105
            "port": "4247",
            "user": f"{user_suffix}@{schema}",
        }
        for schema in ("db", "visibility")
    }
    return ops.testing.Relation(
        "admin",
        id=relation_id,
        remote_app_data={"database_connections": json.dumps(connections)},
        local_app_data={"schema_status": "ready", "migrated_workload_version": PREVIOUS_WORKLOAD_VERSION},
    )


def _two_relation_state(peer_relation, temporal_admin_container):
    """Build a leader state with two admin relations (frontend first, matching second).

    Args:
        peer_relation: The peer relation fixture.
        temporal_admin_container: The workload container fixture.

    Returns:
        The state to run the charm with.
    """
    peer_relation.local_app_data["is_initial_schema_ready"] = "true"
    return ops.testing.State(
        leader=True,
        relations=[peer_relation, _admin_relation(10, "frontend"), _admin_relation(11, "matching")],
        containers=[temporal_admin_container],
    )


def _fake_execute(calls, errors):
    """Record update-schema calls as (user, schema) and raise any error mapped to that pair.

    Args:
        calls: List the (user, schema) pairs are appended to.
        errors: Maps a (user, schema) pair to the stderr its ExecError carries.

    Returns:
        A replacement for charm.execute.
    """

    def execute(container, command, *args, timeout=60):
        """Stand in for charm.execute.

        Args:
            container: Ignored.
            command: The command line's executable.
            args: The command arguments; --user and -d identify the call.
            timeout: Ignored.

        Returns:
            Empty command output.

        Raises:
            ExecError: If an error is mapped to this (user, schema) pair.
        """
        args = list(args)
        if "setup-schema" in args:
            return ""
        user = args[args.index("--user") + 1].split("@")[0]
        schema = args[args.index("-d") + 1].split("/")[-2]
        calls.append((user, schema))
        if (user, schema) in errors:
            raise ExecError([command, "<redacted>"], 1, "", errors[(user, schema)])
        return ""

    return execute


def test_failed_visibility_migration_retries_with_next_relation_user(context, peer_relation, temporal_admin_container):
    calls = []
    errors = {("frontend", "visibility"): "error executing statement: pq: must be owner of table executions_visibility"}
    state = _two_relation_state(peer_relation, temporal_admin_container)

    with patch("charm.execute", side_effect=_fake_execute(calls, errors)):
        result = context.run(context.on.upgrade_charm(), state)

    # db migrates with the first relation's user; visibility is retried with the second one's.
    assert calls == [("frontend", "temporal"), ("frontend", "visibility"), ("matching", "visibility")]
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION


def test_unrelated_error_blocks_without_trying_next_relation_user(context, peer_relation, temporal_admin_container):
    calls = []
    errors = {("frontend", "visibility"): 'Unable to update SQL schema. {"error": "pq: syntax error at or near"}'}
    state = _two_relation_state(peer_relation, temporal_admin_container)

    with patch("charm.execute", side_effect=_fake_execute(calls, errors)):
        result = context.run(context.on.upgrade_charm(), state)

    assert calls == [("frontend", "temporal"), ("frontend", "visibility")]
    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert "not a permission error" in result.unit_status.message


UNREACHABLE_DB = 'Unable to connect to SQL database. {"error": "dial tcp 10.0.0.1:6432: connect: connection refused"}'
BAD_PASSWORD = 'Unable to connect to SQL database. {"error": "pq: password authentication failed for user \\"u\\""}'


def test_unreachable_database_waits_and_defers_without_trying_next_relation_user(
    context, peer_relation, temporal_admin_container
):
    calls = []
    errors = {("frontend", "temporal"): UNREACHABLE_DB}
    state = _two_relation_state(peer_relation, temporal_admin_container)

    with patch("charm.execute", side_effect=_fake_execute(calls, errors)):
        result = context.run(context.on.upgrade_charm(), state)

    assert calls == [("frontend", "temporal")]
    assert isinstance(result.unit_status, ops.WaitingStatus)
    assert result.deferred
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == PREVIOUS_WORKLOAD_VERSION


def test_deferred_migration_completes_once_database_is_back(context, peer_relation, temporal_admin_container):
    state = _two_relation_state(peer_relation, temporal_admin_container)
    with patch("charm.execute", side_effect=_fake_execute([], {("frontend", "temporal"): UNREACHABLE_DB})):
        result = context.run(context.on.upgrade_charm(), state)
    assert result.deferred

    with patch("charm.execute", side_effect=_fake_execute([], {})):
        result = context.run(context.on.update_status(), result)

    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION


def test_bad_credentials_block_instead_of_waiting(context, peer_relation, temporal_admin_container):
    state = _two_relation_state(peer_relation, temporal_admin_container)
    errors = {("frontend", "temporal"): BAD_PASSWORD}

    with patch("charm.execute", side_effect=_fake_execute([], errors)):
        result = context.run(context.on.upgrade_charm(), state)

    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert not result.deferred


@pytest.mark.parametrize("event", ["relation_changed", "pebble_ready"])
def test_readded_admin_relation_on_fresh_db_runs_setup_schema(
    context, upgrade_state, admin_relation, peer_relation, temporal_admin_container, event
):
    removed = context.run(context.on.relation_broken(admin_relation), upgrade_state)
    assert removed.get_relation(peer_relation.id).local_app_data["is_initial_schema_ready"] == "false"

    trigger = getattr(context.on, event)(admin_relation if event == "relation_changed" else temporal_admin_container)
    with patch("charm.execute") as sql:
        result = context.run(trigger, removed)

    assert sum("setup-schema" in call.args for call in sql.call_args_list) == 2
    assert result.unit_status == ops.ActiveStatus()
