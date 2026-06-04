# services/sa_common/sa_common/db/connection.py
import logging
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

log = logging.getLogger(__name__)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Example: "
            "postgresql://gridsnake:dev_password_change_me@localhost:5432/gridsnake"
        )
    return url


def get_conn(autocommit: bool = False) -> psycopg.Connection:
    """
    Open a new connection. Caller is responsible for closing it
    (or use the `transaction` context manager).
    """
    return psycopg.connect(_database_url(), autocommit=autocommit)


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """
    Open a connection, run the block inside a transaction, commit on success,
    rollback on any exception, always close.

    Usage:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    conn = get_conn()
    try:
        with conn:           # commits on success, rolls back on exception
            yield conn
    except Exception:
        log.exception("transaction failed; rolled back")
        raise
    finally:
        conn.close()