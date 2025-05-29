# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

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
