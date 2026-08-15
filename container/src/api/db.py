"""Postgres access for UX-Proto — one table (`projects`) on aw-postgres.

Own database (`ux_proto`) on the shared aw-postgres container (127.0.0.1:5432
inside the aw-sandbox network namespace — see src/api/pg_db.py for the
awserv-side equivalent of this pattern). Plain psycopg (sync), no ORM: one
table doesn't earn SQLAlchemy.
"""

from __future__ import annotations

import os
import time
import uuid

import psycopg
import psycopg.errors

_DB_NAME = "ux_proto"
_ADMIN_URL = os.environ.get(
    "UX_PROTO_ADMIN_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
)
_DB_URL = os.environ.get(
    "UX_PROTO_DB_URL", f"postgresql://postgres:postgres@127.0.0.1:5432/{_DB_NAME}"
)


def ensure_database(retries: int = 10, delay: float = 2.0) -> None:
    """Idempotent CREATE DATABASE — retries while aw-postgres is still booting."""
    last_exc = None
    for _ in range(retries):
        try:
            conn = psycopg.connect(_ADMIN_URL, autocommit=True)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (_DB_NAME,)
                ).fetchone()
                if not exists:
                    conn.execute(f'CREATE DATABASE "{_DB_NAME}"')
            finally:
                conn.close()
            return
        except Exception as exc:  # noqa: BLE001 - retry loop, re-raised below
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"could not reach aw-postgres to create {_DB_NAME}: {last_exc}")


def ensure_schema() -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id UUID PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at TIMESTAMPTZ
            )
            """
        )
        # selected_version: which version (LATEST or a snapshot number, as
        # text) the project's public_url currently points at — persisted so
        # picking a snapshot in the dashboard's version picker also changes
        # what the external full-screen URL shows, not just the in-dashboard
        # iframe. Added after the table already existed in prod, hence the
        # separate ALTER rather than folding into CREATE TABLE above.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS selected_version TEXT NOT NULL DEFAULT 'latest'")
        # Templates — curated/promoted starting points for new projects. A
        # `templates` family can have several coexisting `template_versions`
        # (e.g. "v1", "v2" — free-form labels, not required to be
        # incremental); promoting never overwrites an existing version, it
        # only ever adds a new one (see templates_fs.promote_from_project).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                id UUID PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at TIMESTAMPTZ
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS template_versions (
                id UUID PRIMARY KEY,
                template_id UUID NOT NULL REFERENCES templates(id),
                version_label TEXT NOT NULL,
                dir_name TEXT NOT NULL,
                source_project_slug TEXT,
                source_version TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (template_id, version_label)
            )
            """
        )
        # Provenance recorded on the project at creation time — template_slug
        # + template_version_label are durable TEXT (survive the referenced
        # template_versions row being deleted later), template_version_id is
        # the FK for joins while it still resolves (ON DELETE SET NULL, never
        # CASCADE — deleting a template version must never take a project
        # down with it).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS template_slug TEXT")
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS template_version_label TEXT")
        conn.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS template_version_id UUID "
            "REFERENCES template_versions(id) ON DELETE SET NULL"
        )
        conn.commit()
    finally:
        conn.close()


def get_conn() -> psycopg.Connection:
    return psycopg.connect(_DB_URL, row_factory=psycopg.rows.dict_row)


def init() -> None:
    ensure_database()
    ensure_schema()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def create_project(
    name: str,
    template_slug: str | None = None,
    template_version_label: str | None = None,
    template_version_id: str | None = None,
) -> dict:
    slug = _unique_slug(name)
    row = {
        "id": str(uuid.uuid4()),
        "slug": slug,
        "name": name,
        "status": "active",
        "template_slug": template_slug,
        "template_version_label": template_version_label,
        "template_version_id": template_version_id,
    }
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO projects (id, slug, name, status, template_slug, template_version_label, template_version_id)
            VALUES (%(id)s, %(slug)s, %(name)s, %(status)s, %(template_slug)s, %(template_version_label)s, %(template_version_id)s)
            """,
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(slug)


def _unique_slug(name: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    base = base or "project"
    conn = get_conn()
    try:
        slug = base
        n = 2
        while conn.execute("SELECT 1 FROM projects WHERE slug = %s", (slug,)).fetchone():
            slug = f"{base}-{n}"
            n += 1
        return slug
    finally:
        conn.close()


def list_projects(include_deleted: bool = False) -> list[dict]:
    conn = get_conn()
    try:
        if include_deleted:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = 'active' ORDER BY created_at DESC"
            ).fetchall()
        return rows
    finally:
        conn.close()


def get_project(slug: str) -> dict | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM projects WHERE slug = %s", (slug,)).fetchone()
    finally:
        conn.close()


def soft_delete_project(slug: str) -> dict | None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET status = 'deleted', deleted_at = now(), updated_at = now() WHERE slug = %s",
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(slug)


def restore_project(slug: str) -> dict | None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET status = 'active', deleted_at = NULL, updated_at = now() WHERE slug = %s",
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(slug)


def set_selected_version(slug: str, version: str) -> dict | None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET selected_version = %s, updated_at = now() WHERE slug = %s",
            (version, slug),
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(slug)


# ---------------------------------------------------------------------------
# Templates — curated/promoted starting points, with named coexisting versions
# ---------------------------------------------------------------------------


def get_template(slug: str) -> dict | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM templates WHERE slug = %s AND deleted_at IS NULL", (slug,)
        ).fetchone()
    finally:
        conn.close()


def list_templates() -> list[dict]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM templates WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
    finally:
        conn.close()


def get_or_create_template(slug: str, description: str | None = None) -> dict:
    """First promotion to a never-seen slug auto-creates the family — there is
    no separate "create template" step an agent must call first."""
    existing = get_template(slug)
    if existing:
        return existing
    row = {"id": str(uuid.uuid4()), "slug": slug, "name": slug, "description": description}
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO templates (id, slug, name, description) VALUES (%(id)s, %(slug)s, %(name)s, %(description)s)",
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return get_template(slug)


def get_template_version(template_slug: str, version_label: str) -> dict | None:
    conn = get_conn()
    try:
        return conn.execute(
            """
            SELECT tv.* FROM template_versions tv
            JOIN templates t ON t.id = tv.template_id
            WHERE t.slug = %s AND tv.version_label = %s
            """,
            (template_slug, version_label),
        ).fetchone()
    finally:
        conn.close()


def list_template_versions(template_slug: str) -> list[dict]:
    """Newest first. Empty list if the template exists but has no versions yet."""
    conn = get_conn()
    try:
        return conn.execute(
            """
            SELECT tv.* FROM template_versions tv
            JOIN templates t ON t.id = tv.template_id
            WHERE t.slug = %s
            ORDER BY tv.created_at DESC
            """,
            (template_slug,),
        ).fetchall()
    finally:
        conn.close()


def insert_template_version(
    template_id: str,
    version_label: str,
    dir_name: str,
    source_project_slug: str | None = None,
    source_version: str | None = None,
) -> dict:
    """Raises ValueError if version_label already exists for this template —
    never overwrites (UNIQUE (template_id, version_label) is the backstop)."""
    row = {
        "id": str(uuid.uuid4()),
        "template_id": template_id,
        "version_label": version_label,
        "dir_name": dir_name,
        "source_project_slug": source_project_slug,
        "source_version": source_version,
    }
    conn = get_conn()
    try:
        try:
            conn.execute(
                """
                INSERT INTO template_versions
                    (id, template_id, version_label, dir_name, source_project_slug, source_version)
                VALUES
                    (%(id)s, %(template_id)s, %(version_label)s, %(dir_name)s, %(source_project_slug)s, %(source_version)s)
                """,
                row,
            )
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            raise ValueError(f"version_label {version_label!r} already exists for this template") from None
        return conn.execute(
            "SELECT * FROM template_versions WHERE id = %s", (row["id"],)
        ).fetchone()
    finally:
        conn.close()
