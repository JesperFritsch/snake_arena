# services/orchestrator/orchestrator/scorer_daemon.py
"""Daemon that scores completed matches.

Claims one unscored success match at a time using a *revertable lease*
(scoring_started_at), fetches its bundle, computes scores per the mode's
scoring_config, and writes them into match_participants.metrics. Only on
success does it mark the row as scored_at = NOW().

If anything between the claim and the write fails (transient bundler error,
malformed zip, etc.), the lease is released so a future worker can pick the
match up again. This avoids the silent-data-loss path the previous design
had where one nginx hiccup permanently un-scored a match.

Test matches (mode_id IS NULL) are scored too, using DEFAULT_CONFIG from
the scoring module. The score is informational — never written to a ranked
leaderboard. See docs/09_ranking_system.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event

import psycopg

from sa_common.bundler import IBundler
from sa_common.db.connection import get_conn
from sa_common.db.matches import (
    claim_unscored_match,
    get_match_participants,
    mark_match_scored,
    record_participant_scores,
    release_score_lease,
    reset_stale_score_leases,
)
from sa_common.db.modes import get_mode
from sa_common.scoring import DEFAULT_CONFIG, ScoringConfig, compute_scores
from orchestrator.bundle import read_bundle

log = logging.getLogger(__name__)


@dataclass
class ScorerDaemonConfig:
    bundler: IBundler


def run_one_iteration(conn: psycopg.Connection, config: ScorerDaemonConfig) -> bool:
    """Claim, score, and commit one match. Returns True if work was done."""
    # --- Claim (lease) ---
    with conn.transaction():
        claim = claim_unscored_match(conn)
    if claim is None:
        return False

    log.info(
        "scoring match id=%d mode_id=%s bundle=%s",
        claim.match_id,
        claim.mode_id if claim.mode_id is not None else "test",
        claim.bundle_key,
    )

    # Resolve the scoring config from the mode (or default for test matches).
    # The budget itself comes from the bundle — that's what the runner actually
    # enforced, which is the right denominator for the speed multiplier even
    # if mode.budget_ms gets re-tuned later.
    scoring_cfg = DEFAULT_CONFIG
    if claim.mode_id is not None:
        with conn.transaction():
            mode = get_mode(conn, claim.mode_id)
        if mode is None:
            # Mode was deleted between match creation and scoring — unusual.
            log.warning(
                "match id=%d references missing mode_id=%d; using DEFAULT_CONFIG",
                claim.match_id, claim.mode_id,
            )
        else:
            scoring_cfg = ScoringConfig.from_dict(mode.scoring_config)

    # --- Fetch + parse bundle (any failure releases the lease) ---
    try:
        bundle_bytes = config.bundler.get(claim.bundle_key)
        contents = read_bundle(bundle_bytes)
    except Exception:
        log.exception(
            "failed to fetch/read bundle for match id=%d key=%s; releasing lease",
            claim.match_id, claim.bundle_key,
        )
        with conn.transaction():
            release_score_lease(conn, claim.match_id)
        return True

    # --- Compute scores ---
    try:
        with conn.transaction():
            participants = get_match_participants(conn, claim.match_id)

        if not participants:
            log.warning(
                "match id=%d has no participants; marking scored to skip",
                claim.match_id,
            )
            with conn.transaction():
                mark_match_scored(conn, claim.match_id)
            return True

        scores = compute_scores(contents.exec_times, contents.budget_ms, participants, scoring_cfg)
    except Exception:
        log.exception(
            "scoring computation failed for match id=%d; releasing lease",
            claim.match_id,
        )
        with conn.transaction():
            release_score_lease(conn, claim.match_id)
        return True

    # --- Commit ---
    with conn.transaction():
        record_participant_scores(conn, claim.match_id, scores)
        mark_match_scored(conn, claim.match_id)

    log.info(
        "scored match id=%d: %s",
        claim.match_id,
        ", ".join(f"seat={s.seat} score={s.score:.1f}" for s in scores),
    )
    return True


def run_forever(
    config: ScorerDaemonConfig,
    shutdown: Event,
    wakeup: Event,
) -> None:
    """Event-driven main loop.

    Drains unscored matches (claim → fetch → score → commit), then waits on
    `wakeup` for the next NOTIFY (a new ranked success match) or shutdown.
    Persistent failures hit the MAX_SCORING_ATTEMPTS cap and become invisible
    to the claim query, so this loop is bounded even under pathological input.
    """
    from sa_common.db.notify import CHANNEL_SCORER, start_listener

    log.info("scorer daemon starting (event-driven)")
    start_listener([CHANNEL_SCORER], wakeup, shutdown)

    with get_conn(autocommit=True) as conn:
        # Recover from a worker that died holding leases.
        with conn.transaction():
            n = reset_stale_score_leases(conn)
        if n:
            log.info("scorer: reset %d stale lease(s) at startup", n)

        while not shutdown.is_set():
            wakeup.clear()
            while not shutdown.is_set():
                try:
                    had_work = run_one_iteration(conn, config)
                except psycopg.OperationalError:
                    log.exception("DB connection failed; exiting")
                    raise
                except Exception:
                    log.exception("iteration failed unexpectedly")
                    had_work = False
                if not had_work:
                    break
            if shutdown.is_set():
                break
            wakeup.wait()

    log.info("scorer daemon shut down cleanly")
