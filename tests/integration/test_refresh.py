# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test to ensure successful refreshes from latest track to the 1.23 track."""

import logging

import jubilant

logger = logging.getLogger(__name__)


def test_refresh_from_latest_to_1_23(juju: jubilant.Juju, admin_tools_latest, charm_path, charm_resources):
    """Test to refresh from latest track to the 1.23 track."""
    juju.refresh(
        admin_tools_latest,
        path=charm_path,
        resources=charm_resources,
    )
    juju.wait(jubilant.all_active, error=jubilant.any_error)
