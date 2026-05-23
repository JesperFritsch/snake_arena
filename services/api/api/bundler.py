# services/api/api/bundler.py
"""Shared, process-cached bundler for the API (resolves bundle keys to URLs)."""
from __future__ import annotations

from functools import cache

from sa_common.bundler import IBundler, bundler_from_env


@cache
def get_bundler() -> IBundler:
    return bundler_from_env()
