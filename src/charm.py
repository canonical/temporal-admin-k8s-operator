#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm definition and helpers."""

import functools
import json
import logging
import subprocess

from ops import framework, main
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)


def log_event_handler(method):
    """Log when a event handler method is executed."""

    @functools.wraps(method)
    def decorated(self, event):
        logger.debug(f"running {method.__name__}")
        try:
            return method(self, event)
        finally:
            logger.debug(f"completed {method.__name__}")

    return decorated


class TemporalAdminK8SCharm(CharmBase):
    """Temporal admin charm."""

    _state = framework.StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.name = "temporal-admin"

        # Handle basic charm lifecycle.
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.temporal_admin_pebble_ready, self._on_temporal_admin_pebble_ready)
        self.framework.observe(self.on.tctl_action, self._on_tctl_action)

        # Handle admin:temporal relation.
        self._state.set_default(database_connections=None)
        self.framework.observe(self.on.admin_relation_changed, self._on_admin_relation_changed)

    @log_event_handler
    def _on_install(self, event):
        """Install temporal admin tools."""
        self.unit.status = MaintenanceStatus("installing temporal admin tools")

    @log_event_handler
    def _on_temporal_admin_pebble_ready(self, event):
        """Handle workload being ready."""
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_admin_relation_changed(self, event):
        """Handle changes on the admin:temporal relation.

        Get reported database connection info. Then use that info to set up the
        schema. Then report back that the schema is ready.
        """
        self.unit.status = WaitingStatus(f"handling {event.relation.name} change")
        database_connections = event.relation.data[event.app].get("database_connections")
        self._state.database_connections = json.loads(database_connections) if database_connections else None
        self._setup_db_schemas(event)

    @log_event_handler
    def _on_tctl_action(self, event):
        """Run the tctl command line tool."""
        # TODO(frankban): make this work.
        args = event.params["args"].split()
        cmd = ["tctl"] + args
        event.log(f"running this tctl command: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8")
            event.fail(f"command failed with code {proc.returncode}\n{err}")
            return
        event.set_results({"result": "command succeeded", "output": proc.stdout.decode("utf-8")})

    def _setup_db_schemas(self, event):
        """Initialize the db schemas if db connections info is available."""
        if not self.model.unit.is_leader():
            return

        container = self.unit.get_container(self.name)
        if not container.can_connect():
            event.defer()
            return

        if not self._state.database_connections:
            self.unit.status = BlockedStatus("admin:temporal relation: database connections info not available")
            return

        logger.info("initializing db schemas")
        # TODO(frankban): initialize db schemas and send, via the temporal
        # relation, a "schema_status": "ready" message.

        self.unit.status = ActiveStatus()


if __name__ == "__main__":
    main.main(TemporalAdminK8SCharm)
