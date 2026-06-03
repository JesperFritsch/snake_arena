# services/sa_common/sa_common/db/notify.py
"""Postgres LISTEN/NOTIFY helpers for event-driven daemons.

Each daemon runs a background thread that holds a dedicated connection in
autocommit mode, issues LISTEN on its wakeup channels, and sets a
threading.Event whenever a notification arrives. The daemon's main loop
drains its queue, then waits on that event — no polling, no timeouts.

Triggers fire pg_notify() inside the same transaction as the row change
(see migrations/001.sql), so by the time the listener receives a
notification, the row is committed and visible to the daemon's work query.

If the listener connection drops, the thread reconnects and re-LISTENs,
then fires the wakeup once so the daemon re-drains state that may have
changed during the disconnection.

Channel names are constants in this module; both the trigger DDL and the
daemon code reference them from here so the contract is in one place.
"""
from __future__ import annotations

import logging
import threading
import time

import psycopg

from sa_common.db.connection import _database_url

log = logging.getLogger(__name__)


CHANNEL_SCHEDULER     = "scheduler_wakeup"
CHANNEL_MATCH_RUNNER  = "match_runner_wakeup"
CHANNEL_TEST_RUNNER   = "test_runner_wakeup"


_RECONNECT_BACKOFF_S = 2.0


def start_listener(
    channels: list[str],
    wakeup: threading.Event,
    shutdown: threading.Event,
) -> threading.Thread:
    """Spawn a daemon thread that LISTENs on the given channels.

    Sets `wakeup` once on (re)connect (to flush state that may have changed
    while we weren't subscribed) and once per received notification. Stops
    when `shutdown` is set; on a clean shutdown the thread may still be
    blocked inside conn.notifies() — it's a daemon thread, so the process
    exits without waiting on it.
    """
    t = threading.Thread(
        target=_listener_loop,
        args=(channels, wakeup, shutdown),
        name=f"pg-listener-{'-'.join(channels)}",
        daemon=True,
    )
    t.start()
    return t


def _listener_loop(
    channels: list[str],
    wakeup: threading.Event,
    shutdown: threading.Event,
) -> None:
    while not shutdown.is_set():
        try:
            with psycopg.connect(_database_url(), autocommit=True) as conn:
                for ch in channels:
                    conn.execute(f"LISTEN {ch}")
                log.info("listener: LISTEN %s", channels)
                # Flush whatever may have arrived before we attached.
                wakeup.set()
                for _notify in conn.notifies():
                    if shutdown.is_set():
                        return
                    wakeup.set()
        except psycopg.OperationalError:
            log.warning("listener connection dropped; reconnecting", exc_info=True)
        except Exception:
            log.exception("listener crashed; reconnecting")
        if shutdown.is_set():
            return
        time.sleep(_RECONNECT_BACKOFF_S)
