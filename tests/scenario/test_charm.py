# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import json
import logging
import unittest.mock

import ops
import ops.testing
import pytest

from charm import WORKLOAD_VERSION

logger = logging.getLogger(__name__)


@pytest.fixture()
def all_required_relations(request, admin_relation, peer_relation):
    relations = [peer_relation]

    if not request.node.get_closest_marker("admin_relation_skipped"):
        relations.append(admin_relation)

    return relations


@pytest.fixture()
def state(temporal_admin_container, all_required_relations):
    return ops.testing.State(
        leader=True,
        containers=[temporal_admin_container],
        relations=all_required_relations,
    )


@pytest.mark.admin_relation_skipped
def test_missing_admin_relation(context, state, temporal_admin_container):
    """Block when the admin relation is absent."""
    state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

    assert state_out.unit_status == ops.BlockedStatus("admin:temporal relation: not available")


@pytest.mark.admin_relation_uninitialized
def test_missing_admin_relation_data(context, state, temporal_admin_container):
    """Block when no admin credentials can migrate either schema."""
    state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

    assert isinstance(state_out.unit_status, ops.BlockedStatus)
    assert state_out.unit_status.message.startswith("schema migration incomplete for: db, visibility;")


def test_ready(context, state, temporal_admin_container):
    """Migrate both schemas without adding Pebble services."""
    with unittest.mock.patch("charm.execute") as execute:
        state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

        assert state_out.unit_status == ops.ActiveStatus()
        assert state_out.get_container("temporal-admin").plan.to_dict() == {}

        assert execute.call_count == 4


def test_install_sets_maintenance_without_migrating(context, state):
    """Report installation without running SQL."""
    with unittest.mock.patch("charm.execute") as execute:
        state_out = context.run(context.on.install(), state)

    execute.assert_not_called()
    assert state_out.unit_status == ops.MaintenanceStatus("installing temporal admin tools")


def test_ready_publishes_schema_state_on_admin_relation(context, state, temporal_admin_container, admin_relation):
    """Publish readiness and the workload version after migration."""
    with unittest.mock.patch("charm.execute"):
        state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

    assert state_out.get_relation(admin_relation.id).local_app_data == {
        "schema_status": "ready",
        "migrated_workload_version": WORKLOAD_VERSION,
    }
    assert state_out.workload_version == WORKLOAD_VERSION


@pytest.mark.parametrize("event", ["update_status", "config_changed", "leader_elected"])
def test_routine_events_route_to_reconcile(context, state, event):
    """Reconcile unpublished schemas on routine hooks."""
    with unittest.mock.patch("charm.execute") as execute:
        state_out = context.run(getattr(context.on, event)(), state)

    assert execute.call_count == 4
    assert state_out.unit_status == ops.ActiveStatus()


@pytest.mark.parametrize("event", ["update_status", "config_changed", "leader_elected", "install"])
def test_nonleader_goes_active_without_migrating(context, state, event):
    """Keep connected nonleaders SQL-free, retaining install maintenance."""
    state = dataclasses.replace(state, leader=False)
    with unittest.mock.patch("charm.execute") as execute:
        state_out = context.run(getattr(context.on, event)(), state)

    execute.assert_not_called()
    if event == "install":
        assert state_out.unit_status == ops.MaintenanceStatus("installing temporal admin tools")
    else:
        assert state_out.unit_status == ops.ActiveStatus()


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "nonleader"])
@pytest.mark.parametrize(
    "event",
    [
        "install",
        "update_status",
        "config_changed",
        "leader_elected",
        "pebble_ready",
        "upgrade_charm",
        "relation_changed",
        "peer_created",
    ],
)
def test_container_unavailable_waits_without_migrating(
    context, state, temporal_admin_container, admin_relation, peer_relation, leader, event
):
    """Wait for connectivity on both roles, preserving install and forcing bookkeeping."""
    state = dataclasses.replace(state, leader=leader)
    state = dataclasses.replace(state, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)])
    if event == "pebble_ready":
        trigger = context.on.pebble_ready(temporal_admin_container)
    elif event == "relation_changed":
        trigger = context.on.relation_changed(admin_relation)
    elif event == "peer_created":
        trigger = context.on.relation_created(peer_relation)
    else:
        trigger = getattr(context.on, event)()
    with unittest.mock.patch("charm.execute") as execute:
        state_out = context.run(trigger, state)

    execute.assert_not_called()
    message = "installing temporal admin tools" if event == "install" else "waiting for schema migration container"
    assert state_out.unit_status == ops.MaintenanceStatus(message)
    expected_peer = dict(peer_relation.local_app_data)
    if leader and event in ("upgrade_charm", "relation_changed"):
        expected_peer["forced_migration_pending"] = "true"
    assert state_out.get_relation(peer_relation.id).local_app_data == expected_peer
    assert state_out.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data
    assert state_out.get_container("temporal-admin").plan.to_dict() == {}


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "nonleader"])
@pytest.mark.parametrize("action", ["cli", "setup-schema"])
def test_disconnected_actions_keep_their_own_failure_behavior(
    context, state, temporal_admin_container, admin_relation, peer_relation, leader, action
):
    """Fail disconnected actions without applying nonleader hook maintenance."""
    state = dataclasses.replace(
        state, leader=leader, containers=[dataclasses.replace(temporal_admin_container, can_connect=False)]
    )
    with unittest.mock.patch("charm.execute") as execute, pytest.raises(ops.testing.ActionFailed) as failure:
        context.run(
            context.on.action(action, params={"args": "operator cluster health"} if action == "cli" else {}), state
        )
    execute.assert_not_called()
    if action == "cli":
        expected = "cannot connect to container"
    elif leader:
        expected = "schema migration incomplete: waiting for schema migration container"
    else:
        expected = "schema migration incomplete: setup-schema must be run on the leader unit"
    assert str(failure.value) == expected
    result = failure.value.state
    assert result.unit_status == (
        ops.MaintenanceStatus("waiting for schema migration container")
        if action == "setup-schema" and leader
        else state.unit_status
    )
    expected_peer = dict(peer_relation.local_app_data)
    if action == "setup-schema" and leader:
        expected_peer["forced_migration_pending"] = "true"
    assert result.get_relation(peer_relation.id).local_app_data == expected_peer
    assert result.get_relation(admin_relation.id).local_app_data == admin_relation.local_app_data
    assert result.get_container("temporal-admin").plan.to_dict() == {}


CA_1 = "-----BEGIN CERTIFICATE-----\nCA-ONE\n-----END CERTIFICATE-----"
CA_PATH = "/etc/temporal/certs/frontend-ca.pem"
FRONTEND_HOST = "temporal-k8s.test.svc.cluster.local"


def _cli_state(state, container, tmp_path, host_info_data, *extra_relations):
    """Return a state ready to run the cli action against a mocked `temporal` binary.

    Args:
        state: The base state.
        container: The temporal-admin container.
        tmp_path: Directory mounted at /etc/temporal, to inspect pushed files.
        host_info_data: The temporal-host-info provider's app data, or None for no relation.
        extra_relations: Any further relations to include.

    Returns:
        The state.
    """
    relations = list(state.relations) + list(extra_relations)
    if host_info_data is not None:
        relations.append(ops.testing.Relation("temporal-host-info", remote_app_data=host_info_data))
    container = dataclasses.replace(
        container,
        execs={ops.testing.Exec(["temporal"], stdout="SERVING")},
        mounts={"etc-temporal": ops.testing.Mount(location="/etc/temporal", source=tmp_path)},
    )
    return dataclasses.replace(state, containers=[container], relations=relations)


def _run_cli(context, state):
    """Run the cli action and return the `temporal` command it executed.

    Args:
        context: The scenario context.
        state: The state to run in.

    Returns:
        The executed command, as a list of arguments.
    """
    context.run(context.on.action("cli", params={"args": "operator cluster health"}), state)
    return context.exec_history["temporal-admin"][-1].command


def test_cli_action_plaintext_frontend(context, state, temporal_admin_container, tmp_path):
    """Dial a plaintext frontend without TLS flags."""
    # Without the tls signal the CLI dials the frontend in plaintext, as before.
    state = _cli_state(state, temporal_admin_container, tmp_path, {"host": FRONTEND_HOST, "port": "7233"})

    command = _run_cli(context, state)

    assert command == ["temporal", f"--address={FRONTEND_HOST}:7233", "operator", "cluster", "health"]
    assert context.action_results["output"] == "SERVING"


def test_cli_action_tls_frontend_with_ca(context, state, temporal_admin_container, tmp_path):
    """Push the provider CA and pass single-token TLS flags."""
    # A TLS frontend is dialled over TLS and verified against the CA from the
    # certificates provider, which is pushed into the container for the run.
    # Each flag is a single token: `--tls --tls-ca-path <path>` before the
    # subcommand makes the CLI fail with "unknown flag: --address".
    ca_relation = ops.testing.Relation("receive-ca-cert", remote_app_data={"certificates": json.dumps([CA_1])})
    state = _cli_state(
        state,
        temporal_admin_container,
        tmp_path,
        {"host": FRONTEND_HOST, "port": "7233", "tls": "true"},
        ca_relation,
    )

    command = _run_cli(context, state)

    assert command == [
        "temporal",
        f"--address={FRONTEND_HOST}:7233",
        "--tls=true",
        f"--tls-ca-path={CA_PATH}",
        "operator",
        "cluster",
        "health",
    ]
    assert (tmp_path / "certs" / "frontend-ca.pem").read_text() == CA_1


def test_cli_action_tls_frontend_without_ca_fails(context, state, temporal_admin_container, tmp_path):
    """Fail before executing the CLI when its TLS CA is absent."""
    # Without the CA the frontend can't be verified, so the action fails with an
    # actionable message instead of running a command that can't connect.
    state = _cli_state(
        state, temporal_admin_container, tmp_path, {"host": FRONTEND_HOST, "port": "7233", "tls": "true"}
    )

    with pytest.raises(ops.testing.ActionFailed, match="integrate receive-ca-cert"):
        context.run(context.on.action("cli", params={"args": "operator cluster health"}), state)
    assert not context.exec_history.get("temporal-admin")


def test_cli_action_fails_without_host_info(context, state, temporal_admin_container, tmp_path):
    """Reject CLI actions without frontend connection information."""
    state = _cli_state(state, temporal_admin_container, tmp_path, None)

    with pytest.raises(ops.testing.ActionFailed, match="temporal-host-info relation not established"):
        context.run(context.on.action("cli", params={"args": "operator cluster health"}), state)
