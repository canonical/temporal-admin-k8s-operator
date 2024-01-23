# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


"""Temporal admin charm unit tests."""

# pylint:disable=protected-access

import json
from unittest import TestCase, mock

from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from charm import TemporalAdminK8SCharm
from state import State


class TestCharm(TestCase):
    """Unit tests.

    Attrs:
        maxDiff: Specifies max difference shown by failed tests.
    """

    maxDiff = None

    def setUp(self):
        """Set up for the unit tests."""
        self.harness = Harness(TemporalAdminK8SCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.set_can_connect("temporal-admin", True)
        self.harness.set_leader(True)
        self.harness.begin()

    def test_initial_plan(self):
        """The initial pebble plan is empty."""
        harness = self.harness
        initial_plan = harness.get_container_pebble_plan("temporal-admin").to_dict()
        self.assertEqual(initial_plan, {})

    def test_blocked_by_temporal(self):
        """The charm is blocked without a temporal:admin relation."""
        harness = self.harness

        # Simulate peer relation readiness.
        harness.add_relation("peer", "temporal-admin")

        # Simulate pebble readiness.
        container = harness.model.unit.get_container("temporal-admin")
        harness.charm.on.temporal_admin_pebble_ready.emit(container)

        # The BlockStatus is set with a message.
        self.assertEqual(
            harness.model.unit.status,
            BlockedStatus("admin:temporal relation: database connections info not available"),
        )

    def test_schema_created_but_no_temporal_relation(self):
        """The state is blocked when creating schemas but losing the admin relation."""
        harness = self.harness

        # Simulate peer relation readiness.
        harness.add_relation("peer", "temporal-admin")

        with mock.patch("charm.execute"):
            simulate_lifecycle(harness)

        # The BlockedStatus is set with a message.
        self.assertEqual(harness.model.unit.status, BlockedStatus("admin:temporal relation: not available"))

    def test_ready(self):
        """The pebble plan is correctly generated when the charm is ready."""
        harness = self.harness

        # Simulate peer relation readiness.
        harness.add_relation("peer", "temporal-admin")

        # Add the temporal relation.
        harness.add_relation("admin", "temporal")

        with mock.patch("charm.execute") as execute:
            simulate_lifecycle(harness)
        # Exec is called 4 times: once for schema initializationa and once for
        # migrations for both the temporal and the visibility databases.
        self.assertEqual(execute.call_count, 4)

        # No pebble plans are used by this charm.
        got_plan = harness.get_container_pebble_plan("temporal-admin").to_dict()
        self.assertEqual(got_plan, {})

        # The ActiveStatus is set with no message.
        self.assertEqual(harness.model.unit.status, ActiveStatus())


def simulate_lifecycle(harness):
    """Simulate a healthy charm life-cycle.

    Args:
        harness: ops.testing.Harness object used to simulate charm lifecycle.
    """
    # Simulate pebble readiness.
    container = harness.model.unit.get_container("temporal-admin")
    harness.charm.on.temporal_admin_pebble_ready.emit(container)

    # Simulate temporal readiness and database info provided.
    event = type(
        "Event",
        (),
        {
            "database": "temporal-k8s",
            "master": {
                "dbname": "mydb",
                "host": "myhost",
                "port": "4247",
                "user": "jean-luc",
                "password": "inner-light",
            },
        },
    )
    database_connections = {
        "db": {
            "dbname": "temporal-k8s_db",
            "host": "myhost",
            "password": "inner-light",
            "port": "4247",
            "user": "jean-luc@db",
        },
        "visibility": {
            "dbname": "temporal-k8s_visibility",
            "host": "myhost",
            "password": "inner-light",
            "port": "4247",
            "user": "jean-luc@visibility",
        },
    }
    app = type("App", (), {"name": "temporal-admin-k8s"})()
    relation = type(
        "Relation",
        (),
        {
            "data": {app: {"database_connections": json.dumps(database_connections)}},
            "name": "admin",
            "id": 42,
        },
    )()
    event = type("Event", (), {"app": app, "relation": relation})()
    harness.charm._on_admin_relation_changed(event)


class TestState(TestCase):
    """Unit tests for state.

    Attrs:
        maxDiff: Specifies max difference shown by failed tests.
    """

    maxDiff = None

    def test_get(self):
        """It is possible to retrieve attributes from the state."""
        state = make_state({"foo": json.dumps("bar")})
        self.assertEqual(state.foo, "bar")
        self.assertIsNone(state.bad)

    def test_set(self):
        """It is possible to set attributes in the state."""
        data = {"foo": json.dumps("bar")}
        state = make_state(data)
        state.foo = 42
        state.list = [1, 2, 3]
        self.assertEqual(state.foo, 42)
        self.assertEqual(state.list, [1, 2, 3])
        self.assertEqual(data, {"foo": "42", "list": "[1, 2, 3]"})

    def test_del(self):
        """It is possible to unset attributes in the state."""
        data = {"foo": json.dumps("bar"), "answer": json.dumps(42)}
        state = make_state(data)
        del state.foo
        self.assertIsNone(state.foo)
        self.assertEqual(data, {"answer": "42"})
        # Deleting a name that is not set does not error.
        del state.foo

    def test_is_ready(self):
        """The state is not ready when it is not possible to get relations."""
        state = make_state({})
        self.assertTrue(state.is_ready())

        state = State("myapp", lambda: None)
        self.assertFalse(state.is_ready())


def make_state(data):
    """Create state object.

    Args:
        data: Data to be included in state.

    Returns:
        State object with data.
    """
    app = "myapp"
    rel = type("Rel", (), {"data": {app: data}})()
    return State(app, lambda: rel)
