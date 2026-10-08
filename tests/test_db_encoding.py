"""Regression test for the SQL_ASCII bytes-vs-str bug in db.get_conn().

Root cause (see the Kanban card write-up): container/entrypoint.sh runs
`initdb` with no --encoding/--locale, and the base image has no locale data,
so a fresh cluster silently lands on SQL_ASCII encoding. Under that
negotiated encoding, psycopg's TextLoader refuses to decode TEXT/VARCHAR
columns and returns raw bytes instead of str — which broke
`_require_project()`'s `row["status"] != "active"` check in main.py, since
b'active' != 'active' is always True.

Needs a real reachable Postgres to create a real SQL_ASCII database against
(the bug is in psycopg's live encoding negotiation with an actual server —
not something a fake connection object can reproduce). Points at
UX_PROTO_TEST_ADMIN_DB_URL if set, else the same default db.py itself uses.
Skips if nothing is reachable there, same as every other Postgres-gated test
in this ecosystem.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "src" / "api"))

import db  # noqa: E402

_ADMIN_URL = os.environ.get(
    "UX_PROTO_TEST_ADMIN_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
)


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(_ADMIN_URL, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="no reachable Postgres at UX_PROTO_TEST_ADMIN_DB_URL for this encoding regression test",
)


@pytest.fixture
def sql_ascii_db(monkeypatch):
    """A real, throwaway SQL_ASCII-encoded database with UX-Proto's schema.

    SQL_ASCII can only be created from template0 (template1 already carries
    the cluster's own default encoding/locale) — the same encoding a fresh
    `initdb` with no flags lands on under an empty locale.
    """
    db_name = f"ux_proto_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(_ADMIN_URL, autocommit=True)
    try:
        admin.execute(
            f'CREATE DATABASE "{db_name}" ENCODING \'SQL_ASCII\' '
            "TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"
        )
    finally:
        admin.close()

    db_url = _ADMIN_URL.rsplit("/", 1)[0] + f"/{db_name}"
    monkeypatch.setattr(db, "_DB_URL", db_url)
    db.ensure_schema()
    try:
        yield
    finally:
        cleanup = psycopg.connect(_ADMIN_URL, autocommit=True)
        try:
            cleanup.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            cleanup.close()


def test_get_project_text_columns_are_str_not_bytes_under_sql_ascii(sql_ascii_db):
    created = db.create_project("café façade")
    fetched = db.get_project(created["slug"])

    for col in ("status", "slug", "name", "selected_version"):
        value = fetched[col]
        assert isinstance(value, str), f"{col!r} decoded as {type(value)!r}, not str: {value!r}"

    assert fetched["status"] == "active"
    assert fetched["name"] == "café façade"
