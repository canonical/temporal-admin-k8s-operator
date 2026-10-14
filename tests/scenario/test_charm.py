# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import json
import logging
import unittest.mock

import ops
import ops.testing
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture()
def all_required_relations(request, peer_relation, admin_relation):
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
    state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

    assert state_out.unit_status == ops.BlockedStatus(
        "admin:temporal relation: database connections info not available"
    )


@pytest.mark.admin_relation_uninitialized
def test_missing_admin_relation_data(context, state, temporal_admin_container):
    state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

    assert state_out.unit_status == ops.BlockedStatus(
        "admin:temporal relation: database connections info not available"
    )


def test_ready(context, state, temporal_admin_container):
    with unittest.mock.patch("charm.execute") as execute:
        state_out = context.run(context.on.pebble_ready(temporal_admin_container), state)

        assert state_out.unit_status == ops.ActiveStatus()
        assert state_out.get_container("temporal-admin").plan.to_dict() == {}

        assert execute.call_count == 4


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
    # Without the tls signal the CLI dials the frontend in plaintext, as before.
    state = _cli_state(state, temporal_admin_container, tmp_path, {"host": FRONTEND_HOST, "port": "7233"})

    command = _run_cli(context, state)

    assert command == ["temporal", f"--address={FRONTEND_HOST}:7233", "operator", "cluster", "health"]
    assert context.action_results["output"] == "SERVING"


def test_cli_action_tls_frontend_with_ca(context, state, temporal_admin_container, tmp_path):
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
    # Without the CA the frontend can't be verified, so the action fails with an
    # actionable message instead of running a command that can't connect.
    state = _cli_state(
        state, temporal_admin_container, tmp_path, {"host": FRONTEND_HOST, "port": "7233", "tls": "true"}
    )

    with pytest.raises(ops.testing.ActionFailed, match="integrate receive-ca-cert"):
        context.run(context.on.action("cli", params={"args": "operator cluster health"}), state)
    assert not context.exec_history.get("temporal-admin")


def test_cli_action_fails_without_host_info(context, state, temporal_admin_container, tmp_path):
    state = _cli_state(state, temporal_admin_container, tmp_path, None)

    with pytest.raises(ops.testing.ActionFailed, match="temporal-host-info relation not established"):
        context.run(context.on.action("cli", params={"args": "operator cluster health"}), state)
