#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import json

import ops.testing
import pytest

from charm import TemporalAdminK8SCharm


def pytest_configure(config):
    """Flags that can be configured to modify fixture behavior.

    Used to determine how _state in the peer relation app databag is populated.
    """
    config.addinivalue_line("markers", "admin_relation_skipped")
    config.addinivalue_line("markers", "admin_relation_uninitialized")


@pytest.fixture
def temporal_admin_charm():
    return TemporalAdminK8SCharm


@pytest.fixture(scope="function")
def context(temporal_admin_charm):
    return ops.testing.Context(charm_type=temporal_admin_charm)


@pytest.fixture(scope="function")
def temporal_admin_container():
    return ops.testing.Container("temporal-admin", can_connect=True)


@pytest.fixture
def database_connection_data():
    return {
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


@pytest.fixture(scope="function")
def peer_relation(request, database_connection_data):
    state_data = {}

    if not request.node.get_closest_marker("admin_relation_skipped") and not request.node.get_closest_marker(
        "admin_relation_uninitialized"
    ):
        state_data["database_connections"] = json.dumps(database_connection_data)

    return ops.testing.PeerRelation("peer", local_app_data=state_data)


@pytest.fixture(scope="function")
def admin_relation(request, database_connection_data):
    remote_app_data = (
        {
            "database_connections": json.dumps(database_connection_data),
        }
        if not request.node.get_closest_marker("admin_relation_uninitialized")
        else {}
    )

    return ops.testing.Relation("admin", remote_app_data=remote_app_data)
