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
from scenario.errors import UncaughtCharmError

from charm import WORKLOAD_VERSION


@pytest.fixture
def upgrade_state(admin_relation, temporal_admin_container, peer_relation):
    admin_relation.local_app_data.update(schema_status="ready", migrated_workload_version="1.23.1")
    return ops.testing.State(
        leader=True, relations=[admin_relation, peer_relation], containers=[temporal_admin_container]
    )


@pytest.fixture
def migrated_state(admin_relation, temporal_admin_container, peer_relation):
    """A leader state whose admin relation already publishes the current version."""
    admin_relation.local_app_data.update(schema_status="ready", migrated_workload_version=WORKLOAD_VERSION)
    return ops.testing.State(
        leader=True, relations=[admin_relation, peer_relation], containers=[temporal_admin_container]
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
    """A failed migration is never published, so the next reconcile redoes the migration."""
    outcomes = [None] * 4
    outcomes[failed_call] = RuntimeError("migration interrupted")
    with patch("charm.execute", side_effect=outcomes):
        result = context.run(context.on.upgrade_charm(), upgrade_state)
    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": "1.23.1",
    }
    # The published version is stale, so the next reconcile resumes the migration.
    with patch("charm.execute") as sql:
        result = context.run(context.on.pebble_ready(temporal_admin_container), result)
    assert result.unit_status == ops.ActiveStatus()
    assert sql.call_count == 4
    assert result.get_relation(admin_relation.id).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION
    with patch("charm.execute") as sql:
        context.run(context.on.pebble_ready(temporal_admin_container), result)
    sql.assert_not_called()


def test_container_unavailable_waits_and_keeps_old_version_until_retried(
    context, upgrade_state, admin_relation, temporal_admin_container
):
    state = dataclasses.replace(
        upgrade_state, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
    )
    with patch("charm.execute") as sql:
        result = context.run(context.on.upgrade_charm(), state)
    sql.assert_not_called()
    assert result.unit_status == ops.MaintenanceStatus("waiting for schema migration container")
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": "1.23.1",
    }

    # Nothing is deferred: the next event with a reachable container completes the migration.
    reachable = dataclasses.replace(result, containers=[temporal_admin_container])
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), reachable)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION


def test_pebble_ready_resumes_pending_upgrade(context, upgrade_state, admin_relation, temporal_admin_container):
    with patch("charm.execute") as sql:
        result = context.run(context.on.pebble_ready(temporal_admin_container), upgrade_state)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION


@pytest.mark.parametrize("leader", [True, False])
def test_pebble_ready_skips_migration_when_already_done(context, migrated_state, temporal_admin_container, leader):
    with patch("charm.execute") as sql:
        result = context.run(
            context.on.pebble_ready(temporal_admin_container), dataclasses.replace(migrated_state, leader=leader)
        )
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


def test_nonleader_does_not_migrate(context, upgrade_state, admin_relation):
    with patch("charm.execute") as sql:
        result = context.run(context.on.upgrade_charm(), dataclasses.replace(upgrade_state, leader=False))
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data["migrated_workload_version"] == "1.23.1"


@pytest.mark.parametrize("event", ["update_status", "config_changed", "leader_elected"])
def test_stale_published_version_is_reconciled_on_routine_events(context, upgrade_state, admin_relation, event):
    with patch("charm.execute") as sql:
        result = context.run(getattr(context.on, event)(), upgrade_state)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }


@pytest.mark.parametrize("event", ["update_status", "config_changed", "leader_elected"])
def test_routine_events_do_not_migrate_when_published_state_is_current(context, migrated_state, event):
    with patch("charm.execute") as sql:
        result = context.run(getattr(context.on, event)(), migrated_state)
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


def test_reconcile_is_idempotent(context, upgrade_state, admin_relation):
    with patch("charm.execute") as sql:
        first = context.run(context.on.update_status(), upgrade_state)
        assert sql.call_count == 4
        second = context.run(context.on.update_status(), first)
    assert sql.call_count == 4
    assert second.unit_status == ops.ActiveStatus()
    assert second.get_relation(admin_relation.id).local_app_data == first.get_relation(admin_relation.id).local_app_data


@pytest.mark.parametrize("status", [None, "pending"])
def test_unready_schema_status_is_migrated(context, migrated_state, admin_relation, peer_relation, status):
    local_data = {"migrated_workload_version": WORKLOAD_VERSION}
    if status:
        local_data["schema_status"] = status
    relation = dataclasses.replace(admin_relation, local_app_data=local_data)
    with patch("charm.execute") as sql:
        result = context.run(
            context.on.update_status(), dataclasses.replace(migrated_state, relations=[relation, peer_relation])
        )
    assert sql.call_count == 4
    assert result.get_relation(admin_relation.id).local_app_data["schema_status"] == "ready"


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
        local_app_data={"schema_status": "ready", "migrated_workload_version": "1.23.1"},
    )


def _two_relation_state(temporal_admin_container):
    """Build a leader state with two admin relations (frontend first, matching second).

    Args:
        temporal_admin_container: The workload container fixture.

    Returns:
        The state to run the charm with.
    """
    return ops.testing.State(
        leader=True,
        relations=[_admin_relation(10, "frontend"), _admin_relation(11, "matching"), ops.testing.PeerRelation("peer")],
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


def test_failed_visibility_migration_retries_with_next_relation_user(context, temporal_admin_container):
    calls = []
    errors = {("frontend", "visibility"): "error executing statement: pq: must be owner of table executions_visibility"}
    state = _two_relation_state(temporal_admin_container)

    with patch("charm.execute", side_effect=_fake_execute(calls, errors)):
        result = context.run(context.on.upgrade_charm(), state)

    # db migrates with the first relation's user; visibility is retried with the second one's.
    assert calls == [("frontend", "temporal"), ("frontend", "visibility"), ("matching", "visibility")]
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION


def test_unrelated_error_blocks_without_trying_next_relation_user(context, temporal_admin_container):
    calls = []
    errors = {("frontend", "visibility"): 'Unable to update SQL schema. {"error": "pq: syntax error at or near"}'}
    state = _two_relation_state(temporal_admin_container)

    with patch("charm.execute", side_effect=_fake_execute(calls, errors)):
        result = context.run(context.on.upgrade_charm(), state)

    assert calls == [("frontend", "temporal"), ("frontend", "visibility")]
    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert "not a permission error" in result.unit_status.message


UNREACHABLE_DB = 'Unable to connect to SQL database. {"error": "dial tcp 10.0.0.1:6432: connect: connection refused"}'
BAD_PASSWORD = 'Unable to connect to SQL database. {"error": "pq: password authentication failed for user \\"u\\""}'


def test_unreachable_database_waits_without_trying_next_relation_user(context, temporal_admin_container):
    calls = []
    errors = {("frontend", "temporal"): UNREACHABLE_DB}
    state = _two_relation_state(temporal_admin_container)

    with patch("charm.execute", side_effect=_fake_execute(calls, errors)):
        result = context.run(context.on.upgrade_charm(), state)

    assert calls == [("frontend", "temporal")]
    assert result.unit_status == ops.WaitingStatus("waiting for the database to be reachable")
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == "1.23.1"


def test_migration_completes_on_next_event_once_database_is_back(context, temporal_admin_container):
    state = _two_relation_state(temporal_admin_container)
    with patch("charm.execute", side_effect=_fake_execute([], {("frontend", "temporal"): UNREACHABLE_DB})):
        result = context.run(context.on.upgrade_charm(), state)
    assert isinstance(result.unit_status, ops.WaitingStatus)

    with patch("charm.execute", side_effect=_fake_execute([], {})):
        result = context.run(context.on.update_status(), result)

    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == WORKLOAD_VERSION


def test_bad_credentials_block_instead_of_waiting(context, temporal_admin_container):
    state = _two_relation_state(temporal_admin_container)
    errors = {("frontend", "temporal"): BAD_PASSWORD}

    with patch("charm.execute", side_effect=_fake_execute([], errors)):
        result = context.run(context.on.upgrade_charm(), state)

    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert result.get_relation(10).local_app_data["migrated_workload_version"] == "1.23.1"


@pytest.mark.parametrize("event", ["relation_changed", "pebble_ready", "update_status"])
def test_recreated_admin_relation_on_fresh_db_runs_setup_schema(
    context, migrated_state, admin_relation, temporal_admin_container, peer_relation, event
):
    removed = context.run(context.on.relation_broken(admin_relation), migrated_state)
    assert removed.unit_status == ops.BlockedStatus("admin:temporal relation: not available")

    # A new relation carries no published schema state, so the schema is set up again.
    recreated = ops.testing.Relation("admin", remote_app_data=admin_relation.remote_app_data)
    assert not recreated.local_app_data
    state = dataclasses.replace(removed, relations=[recreated, removed.get_relation(peer_relation.id)])
    trigger = {
        "relation_changed": lambda: context.on.relation_changed(recreated),
        "pebble_ready": lambda: context.on.pebble_ready(temporal_admin_container),
        "update_status": context.on.update_status,
    }[event]()
    with patch("charm.execute") as sql:
        result = context.run(trigger, state)

    assert sum("setup-schema" in call.args for call in sql.call_args_list) == 2
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(recreated.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }


def test_relation_changed_remigrates_even_when_published_state_is_current(context, migrated_state, admin_relation):
    with patch("charm.execute") as sql:
        result = context.run(context.on.relation_changed(admin_relation), migrated_state)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()


FORCED_EVENTS = ["upgrade_charm", "relation_changed", "setup_schema_action"]
SYNTAX_ERROR = 'Unable to update SQL schema. {"error": "pq: syntax error at or near"}'


def _forced_event(context, name, admin_relation):
    """Build one of the events that force a migration.

    Args:
        context: The scenario context.
        name: One of FORCED_EVENTS.
        admin_relation: The admin relation, for the relation-changed event.

    Returns:
        The event to run.
    """
    if name == "upgrade_charm":
        return context.on.upgrade_charm()
    if name == "relation_changed":
        return context.on.relation_changed(admin_relation)
    return context.on.action("setup-schema")


def _run(context, event, state):
    """Run an event and return the resulting state, also when an action fails.

    Args:
        context: The scenario context.
        event: The event to run.
        state: The state to run it in.

    Returns:
        The state after the event.
    """
    try:
        return context.run(event, state)
    except ops.testing.ActionFailed as failure:
        return failure.state


@pytest.mark.parametrize("forced", FORCED_EVENTS)
@pytest.mark.parametrize(
    "stderr, status_type, message",
    [
        (UNREACHABLE_DB, ops.WaitingStatus, "waiting for the database to be reachable"),
        (SYNTAX_ERROR, ops.BlockedStatus, "schema db migration failed (not a permission error)"),
    ],
    ids=["unreachable-waiting", "unrelated-blocked"],
)
def test_failed_forced_migration_retries_waiting_but_preserves_blocked(
    context, migrated_state, admin_relation, peer_relation, forced, stderr, status_type, message
):
    """Update-status retries Waiting failures, but only explicit events retry Blocked failures."""
    failing = {("jean-luc", "temporal"): stderr}
    calls = []
    with patch("charm.execute", side_effect=_fake_execute(calls, failing)):
        result = _run(context, _forced_event(context, forced, admin_relation), migrated_state)
    assert isinstance(result.unit_status, status_type)
    assert result.unit_status.message.startswith(message)
    assert calls
    assert json.loads(result.get_relation(peer_relation.id).local_app_data["forced_migration_pending"]) is True
    assert result.get_relation(peer_relation.id).local_app_data.get("migration_blocked_message", "") == (
        result.unit_status.message if status_type is ops.BlockedStatus else ""
    )
    failure_status = result.unit_status
    published = {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }
    assert result.get_relation(admin_relation.id).local_app_data == published

    # Blocked failures are preserved even when SQL would now succeed.
    for _ in range(2):
        calls.clear()
        errors = failing if status_type is ops.WaitingStatus else {}
        with patch("charm.execute", side_effect=_fake_execute(calls, errors)) as sql:
            result = context.run(context.on.update_status(), result)
        if status_type is ops.WaitingStatus:
            assert calls == [("jean-luc", "temporal")]
        else:
            sql.assert_not_called()
        assert result.unit_status == failure_status
        assert result.get_relation(admin_relation.id).local_app_data == published

    calls.clear()
    retry = (
        context.on.update_status()
        if status_type is ops.WaitingStatus
        else _forced_event(context, forced, admin_relation)
    )
    with patch("charm.execute", side_effect=_fake_execute(calls, {})):
        result = _run(context, retry, result)
    assert calls == [("jean-luc", "temporal"), ("jean-luc", "visibility")]
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }
    assert result.get_relation(peer_relation.id).local_app_data == {
        "forced_migration_pending": "false",
    }

    # Once it succeeded nothing is pending any more.
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), result)
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


@pytest.mark.parametrize("prerequisite", ["container", "admin_relation"])
def test_blocked_migration_does_not_freeze_prerequisite_status(
    context, migrated_state, admin_relation, temporal_admin_container, peer_relation, prerequisite
):
    with patch("charm.execute", side_effect=_fake_execute([], {("jean-luc", "temporal"): SYNTAX_ERROR})):
        blocked = context.run(context.on.upgrade_charm(), migrated_state)
    assert isinstance(blocked.unit_status, ops.BlockedStatus)
    if prerequisite == "container":
        unavailable = dataclasses.replace(
            blocked, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
        )
        expected = ops.MaintenanceStatus("waiting for schema migration container")
    else:
        unavailable = dataclasses.replace(blocked, relations=[blocked.get_relation(peer_relation.id)])
        expected = ops.BlockedStatus("admin:temporal relation: not available")

    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), unavailable)
        assert result.unit_status == expected
        restored = dataclasses.replace(result, containers=blocked.containers, relations=blocked.relations)
        result = context.run(context.on.update_status(), restored)
    sql.assert_not_called()
    assert result.unit_status == blocked.unit_status
    assert (
        result.get_relation(admin_relation.id).local_app_data == blocked.get_relation(admin_relation.id).local_app_data
    )


@pytest.mark.parametrize(
    "forced, prerequisite",
    [(forced, "container") for forced in FORCED_EVENTS]
    + [("upgrade_charm", "admin_relation"), ("setup_schema_action", "admin_relation")],
)
def test_forcing_event_clears_blocked_failure_before_prerequisites(
    context, migrated_state, admin_relation, temporal_admin_container, peer_relation, forced, prerequisite
):
    with patch("charm.execute", side_effect=_fake_execute([], {("jean-luc", "temporal"): SYNTAX_ERROR})):
        blocked = context.run(context.on.upgrade_charm(), migrated_state)
    assert isinstance(blocked.unit_status, ops.BlockedStatus)
    if prerequisite == "container":
        unavailable = dataclasses.replace(
            blocked, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
        )
        expected = ops.MaintenanceStatus("waiting for schema migration container")
    else:
        unavailable = dataclasses.replace(blocked, relations=[blocked.get_relation(peer_relation.id)])
        expected = ops.BlockedStatus("admin:temporal relation: not available")
    with patch("charm.execute") as sql:
        result = _run(context, _forced_event(context, forced, admin_relation), unavailable)
    sql.assert_not_called()
    assert result.unit_status == expected
    assert result.get_relation(peer_relation.id).local_app_data == {
        "forced_migration_pending": "true",
    }

    restored = dataclasses.replace(
        result,
        containers=blocked.containers,
        relations=[blocked.get_relation(admin_relation.id), result.get_relation(peer_relation.id)],
    )
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), restored)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert (
        result.get_relation(admin_relation.id).local_app_data == blocked.get_relation(admin_relation.id).local_app_data
    )
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), result)
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


@pytest.mark.parametrize("stderr", [UNREACHABLE_DB, SYNTAX_ERROR], ids=["waiting", "blocked"])
def test_new_leader_recovers_migration_from_shared_app_data(
    context, migrated_state, admin_relation, peer_relation, stderr
):
    """A fresh leader uses peer app data, not the previous leader's unit-local state."""
    with patch("charm.execute", side_effect=_fake_execute([], {("jean-luc", "temporal"): stderr})):
        failed = context.run(context.on.upgrade_charm(), migrated_state)
    if stderr == UNREACHABLE_DB:
        assert failed.unit_status == ops.WaitingStatus("waiting for the database to be reachable")
    else:
        assert isinstance(failed.unit_status, ops.BlockedStatus)
    shared_data = failed.get_relation(peer_relation.id).local_app_data
    assert json.loads(shared_data["forced_migration_pending"]) is True
    assert shared_data.get("migration_blocked_message", "") == (
        failed.unit_status.message if stderr == SYNTAX_ERROR else ""
    )
    assert failed.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data

    new_leader = ops.testing.State(
        leader=True,
        relations=[
            dataclasses.replace(
                admin_relation, local_app_data=dict(failed.get_relation(admin_relation.id).local_app_data)
            ),
            dataclasses.replace(peer_relation, local_app_data=dict(shared_data)),
        ],
        containers=migrated_state.containers,
    )
    assert not new_leader.stored_states
    with patch("charm.execute") as sql:
        result = context.run(context.on.leader_elected(), new_leader)
        if stderr == UNREACHABLE_DB:
            assert sql.call_count == 4
            assert result.unit_status == ops.ActiveStatus()
            assert result.get_relation(peer_relation.id).local_app_data == {
                "forced_migration_pending": "false",
            }
        else:
            sql.assert_not_called()
            assert result.unit_status == failed.unit_status
            for event in (
                context.on.update_status(),
                context.on.relation_created(result.get_relation(peer_relation.id)),
            ):
                result = context.run(event, result)
                sql.assert_not_called()
                assert result.unit_status == failed.unit_status
                assert result.get_relation(peer_relation.id).local_app_data == shared_data
        assert result.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data
        assert result.get_container("temporal-admin").plan.to_dict() == {}

    if stderr == SYNTAX_ERROR:
        with patch("charm.execute") as sql:
            result = context.run(context.on.action("setup-schema"), result)
        assert sql.call_count == 4
        assert result.unit_status == ops.ActiveStatus()
        assert result.get_relation(peer_relation.id).local_app_data == {
            "forced_migration_pending": "false",
        }
        assert result.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data

    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), result)
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


@pytest.mark.parametrize("event", ["update_status", "leader_elected", "upgrade_charm", "setup_schema_action"])
def test_missing_peer_blocks_without_sql_and_peer_created_recovers(
    context, upgrade_state, admin_relation, peer_relation, event
):
    """An unavailable peer blocks explicitly; its creation reconciles the stale schema."""
    state = dataclasses.replace(upgrade_state, relations=[admin_relation])
    trigger = (
        context.on.update_status()
        if event == "update_status"
        else (
            context.on.leader_elected() if event == "leader_elected" else _forced_event(context, event, admin_relation)
        )
    )
    with patch("charm.execute") as sql:
        result = _run(context, trigger, state)
    sql.assert_not_called()
    assert result.unit_status == ops.BlockedStatus("peer relation unavailable")
    assert result.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data

    state = dataclasses.replace(result, relations=[result.get_relation(admin_relation.id), peer_relation])
    with patch("charm.execute") as sql:
        result = context.run(context.on.relation_created(peer_relation), state)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }
    assert result.get_relation(peer_relation.id).local_app_data == {
        "forced_migration_pending": "false",
    }
    assert result.get_container("temporal-admin").plan.to_dict() == {}


@pytest.mark.parametrize(
    "event", ["update_status", "config_changed", "upgrade_charm", "setup_schema_action", "peer_created"]
)
def test_nonleader_never_writes_shared_migration_data(context, migrated_state, admin_relation, peer_relation, event):
    """Nonleaders leave even pending, blocked leader bookkeeping untouched."""
    data = {"forced_migration_pending": "true", "migration_blocked_message": "previous leader failed"}
    peer = dataclasses.replace(peer_relation, local_app_data=data)
    state = dataclasses.replace(migrated_state, leader=False, relations=[admin_relation, peer])
    if event == "peer_created":
        trigger = context.on.relation_created(peer)
    elif event in ("update_status", "config_changed"):
        trigger = getattr(context.on, event)()
    else:
        trigger = _forced_event(context, event, admin_relation)
    with patch("charm.execute") as sql:
        result = _run(context, trigger, state)
    sql.assert_not_called()
    assert result.unit_status == (state.unit_status if event == "setup_schema_action" else ops.ActiveStatus())
    assert result.get_relation(peer.id).local_app_data == data
    assert result.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data
    assert result.get_container("temporal-admin").plan.to_dict() == {}


def test_publication_failure_propagates_without_clearing_pending(
    context, migrated_state, peer_relation, admin_relation
):
    """Successful SQL alone must not clear the pending marker if publication raises."""
    observed_bags = []

    def fail_publication(charm, relations):
        """Retain the live databags to inspect after the exception unwinds.

        Args:
            charm: The running charm.
            relations: Admin relations whose readiness would be published.

        Raises:
            RuntimeError: To simulate failed publication after successful SQL.
        """
        peer = charm.model.get_relation("peer", peer_relation.id)
        observed_bags.extend([peer.data[charm.app], relations[0].data[charm.app]])
        raise RuntimeError("publication failed")

    with patch("charm.execute") as sql, patch(
        "charm.TemporalAdminK8SCharm._publish_schema_ready", autospec=True, side_effect=fail_publication
    ), pytest.raises(UncaughtCharmError, match="publication failed"):
        context.run(context.on.upgrade_charm(), migrated_state)
    assert sql.call_count == 4
    assert dict(observed_bags[0]) == {"forced_migration_pending": "true"}
    assert dict(observed_bags[1]) == admin_relation.local_app_data


@pytest.mark.parametrize("event", ["config_changed", "pebble_ready"])
def test_blocked_migration_retried_by_other_hook_can_wait_then_retry_on_update_status(
    context, migrated_state, admin_relation, temporal_admin_container, event
):
    with patch("charm.execute", side_effect=_fake_execute([], {("jean-luc", "temporal"): SYNTAX_ERROR})):
        blocked = context.run(context.on.upgrade_charm(), migrated_state)
    assert isinstance(blocked.unit_status, ops.BlockedStatus)
    trigger = (
        context.on.pebble_ready(temporal_admin_container) if event == "pebble_ready" else context.on.config_changed()
    )
    with patch("charm.execute", side_effect=_fake_execute([], {("jean-luc", "temporal"): UNREACHABLE_DB})) as sql:
        waiting = context.run(trigger, blocked)
    assert sql.call_count == 2
    assert waiting.unit_status == ops.WaitingStatus("waiting for the database to be reachable")
    assert (
        waiting.get_relation(admin_relation.id).local_app_data == blocked.get_relation(admin_relation.id).local_app_data
    )

    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), waiting)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert (
        result.get_relation(admin_relation.id).local_app_data == blocked.get_relation(admin_relation.id).local_app_data
    )
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), result)
    sql.assert_not_called()
    assert result.unit_status == ops.ActiveStatus()


def test_unrelated_initial_blocked_status_does_not_suppress_sql(context, upgrade_state, admin_relation):
    state = dataclasses.replace(upgrade_state, unit_status=ops.BlockedStatus("unrelated failure"))
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), state)
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }


@pytest.mark.parametrize("forced", FORCED_EVENTS)
def test_forced_migration_cut_short_by_container_runs_once_it_is_reachable(
    context, migrated_state, admin_relation, temporal_admin_container, forced
):
    unreachable = dataclasses.replace(temporal_admin_container, can_connect=False)
    with patch("charm.execute") as sql:
        result = _run(
            context,
            _forced_event(context, forced, admin_relation),
            dataclasses.replace(migrated_state, containers=[unreachable]),
        )
    sql.assert_not_called()
    assert result.unit_status == ops.MaintenanceStatus("waiting for schema migration container")

    with patch("charm.execute") as sql:
        result = context.run(
            context.on.pebble_ready(temporal_admin_container),
            dataclasses.replace(result, containers=[temporal_admin_container]),
        )
    assert sql.call_count == 4
    assert result.unit_status == ops.ActiveStatus()

    with patch("charm.execute") as sql:
        context.run(context.on.update_status(), result)
    sql.assert_not_called()


def test_setup_schema_on_non_leader_fails_without_peer_relation_wording(context, migrated_state):
    state = dataclasses.replace(migrated_state, leader=False)
    with patch("charm.execute") as sql, pytest.raises(ops.testing.ActionFailed) as failure:
        context.run(context.on.action("setup-schema"), state)
    sql.assert_not_called()
    assert str(failure.value) == "schema migration incomplete: setup-schema must be run on the leader unit"
    assert "peer" not in str(failure.value)


def test_setup_schema_on_leader_reports_the_status_message_on_failure(context, migrated_state):
    failing = {("jean-luc", "temporal"): UNREACHABLE_DB}
    with patch("charm.execute", side_effect=_fake_execute([], failing)), pytest.raises(
        ops.testing.ActionFailed
    ) as failure:
        context.run(context.on.action("setup-schema"), migrated_state)
    assert str(failure.value) == "schema migration incomplete: waiting for the database to be reachable"


def _statuses_during_migration(context, event, state):
    """Run an event, recording the unit status each time the migration executes a command.

    Scenario only exposes the final status, so the status is read inside the patched command.

    Args:
        context: The scenario context.
        event: The event to run.
        state: The state to run it in.

    Returns:
        The statuses seen by each command and the resulting state.
    """
    seen = []
    with context(event, state) as manager:

        def execute(*args, **kwargs):
            """Record the unit status seen by a migration command.

            Args:
                args: Ignored.
                kwargs: Ignored.

            Returns:
                Empty command output.
            """
            seen.append(manager.charm.unit.status)
            return ""

        with patch("charm.execute", side_effect=execute):
            result = manager.run()
    return seen, result


def test_admin_relation_changed_shows_handling_admin_change_during_migration(context, migrated_state, admin_relation):
    seen, result = _statuses_during_migration(context, context.on.relation_changed(admin_relation), migrated_state)
    assert seen == [ops.WaitingStatus("handling admin change")] * 4
    assert result.unit_status == ops.ActiveStatus()


def test_admin_relation_changed_failure_replaces_the_interim_status(context, migrated_state, admin_relation):
    failing = {("jean-luc", "temporal"): UNREACHABLE_DB}
    with patch("charm.execute", side_effect=_fake_execute([], failing)):
        result = context.run(context.on.relation_changed(admin_relation), migrated_state)
    assert result.unit_status == ops.WaitingStatus("waiting for the database to be reachable")


@pytest.mark.parametrize("event", ["upgrade_charm", "update_status", "pebble_ready"])
def test_other_migrating_events_set_no_interim_status(context, upgrade_state, temporal_admin_container, event):
    trigger = (
        context.on.pebble_ready(temporal_admin_container) if event == "pebble_ready" else getattr(context.on, event)()
    )
    seen, result = _statuses_during_migration(context, trigger, upgrade_state)
    assert len(seen) == 4
    assert all(isinstance(status, ops.UnknownStatus) for status in seen)
    assert result.unit_status == ops.ActiveStatus()
