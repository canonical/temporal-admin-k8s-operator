# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exercise the schema readiness contract during refresh and recovery."""

import dataclasses
from unittest.mock import patch

import ops
import ops.testing
import pytest

from charm import WORKLOAD_VERSION


@pytest.fixture
def upgrade_state(peer_relation, admin_relation, temporal_admin_container):
    peer_relation.local_app_data["is_initial_schema_ready"] = "true"
    admin_relation.local_app_data.update(schema_status="ready", schema_version="1.23.1")
    return ops.testing.State(
        leader=True, relations=[peer_relation, admin_relation], containers=[temporal_admin_container]
    )


def test_refresh_publishes_version_after_both_migrations(context, upgrade_state, admin_relation):
    with patch("charm.execute") as sql:
        result = context.run(context.on.upgrade_charm(), upgrade_state)
    assert result.unit_status == ops.ActiveStatus()
    assert result.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "schema_version": WORKLOAD_VERSION,
    }
    assert sql.call_count == 2
    assert all("update-schema" in call.args for call in sql.call_args_list)


@pytest.mark.parametrize("failed_call", [0, 1])
def test_failed_migration_revokes_readiness_and_retries(context, upgrade_state, admin_relation, failed_call):
    outcomes = [None, None]
    outcomes[failed_call] = RuntimeError("migration interrupted")
    with patch("charm.execute", side_effect=outcomes):
        result = context.run(context.on.upgrade_charm(), upgrade_state)
    assert isinstance(result.unit_status, ops.BlockedStatus)
    assert result.get_relation(admin_relation.id).local_app_data == {"schema_status": "failed"}
    with patch("charm.execute") as sql:
        result = context.run(context.on.update_status(), result)
    assert result.unit_status == ops.ActiveStatus()
    assert sql.call_count == 2
    assert result.get_relation(admin_relation.id).local_app_data["schema_version"] == WORKLOAD_VERSION
    with patch("charm.execute") as sql:
        context.run(context.on.update_status(), result)
    sql.assert_not_called()


def test_container_unavailable_revokes_readiness(context, upgrade_state, admin_relation, temporal_admin_container):
    state = dataclasses.replace(
        upgrade_state, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
    )
    result = context.run(context.on.upgrade_charm(), state)
    assert result.get_relation(admin_relation.id).local_app_data == {"schema_status": "migrating"}
    assert result.deferred


def test_pebble_ready_does_not_republish_old_version(context, upgrade_state, temporal_admin_container):
    with patch("charm.execute") as sql:
        result = context.run(context.on.pebble_ready(temporal_admin_container), upgrade_state)
    assert sql.call_count == 2
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


def test_pre_upgrade_check_reports_target_and_current_version(context, peer_relation, upgrade_state, admin_relation):
    peer_relation.local_app_data["schema_workload_version"] = '"1.23.1"'
    result = context.run(context.on.action("pre-upgrade-check"), upgrade_state)
    assert not result.deferred
    results = context.action_results
    assert results["target-schema-version"] == WORKLOAD_VERSION
    assert results["current-schema-version"] == "1.23.1"
    assert results["database-connectivity"] == "ok"
    assert results["backup-verified"] == "false"
    assert "does not verify" in results["warning"]


@pytest.mark.admin_relation_uninitialized
def test_pre_upgrade_check_fails_without_database_connectivity(context, peer_relation, admin_relation, temporal_admin_container):
    state = ops.testing.State(
        leader=True, relations=[peer_relation, admin_relation], containers=[temporal_admin_container]
    )
    with pytest.raises(ops.testing.ActionFailed):
        context.run(context.on.action("pre-upgrade-check"), state)
    assert context.action_results["database-connectivity"] != "ok"
