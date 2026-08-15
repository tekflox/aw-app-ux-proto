"""On-disk layout for UX-Proto templates — data/ux-proto/templates/<slug>/<dir_name>/.

Mirrors projects_fs.py's "filesystem is the source of truth" convention, one
level up: a template *version* directory has the exact same shape as a
project's `latest/` (frontend/ + backend.py), because that's literally what
it's a copy of.

    <slug>/
      <dir_name>/        <- one immutable copy per named version (e.g. "v1"),
        frontend/            made by promote_from_project(); never mutated
        backend.py           in place — evolving a template always adds a
      <dir_name>/           new sibling dir_name, never overwrites one.
      ...

dir_name is DERIVED from the version_label (slugified), not a path an agent
supplies directly — see safe_dir_name.
"""

from __future__ import annotations

import os
import shutil

TEMPLATES_ROOT = os.environ.get("UX_PROTO_TEMPLATES_ROOT", "/data/templates")


def template_dir(slug: str) -> str:
    return os.path.join(TEMPLATES_ROOT, slug)


def template_version_dir(slug: str, dir_name: str) -> str:
    return os.path.join(template_dir(slug), dir_name)


def safe_dir_name(version_label: str) -> str:
    """Slugify a free-form version_label into a directory name — the only
    path segment derived from agent input, so this must never allow `..` or
    an absolute path through regardless of what's typed."""
    base = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in version_label.strip().lower())
    while "--" in base:
        base = base.replace("--", "-")
    base = base.strip("-")
    if not base:
        raise ValueError(f"version_label produces an empty directory name: {version_label!r}")
    return base


def promote_from_project(src_dir: str, slug: str, dir_name: str) -> None:
    """Copy a project's `latest/` (or any version dir) into a new template
    version. Never overwrites — raises FileExistsError if dir_name is
    already taken for this template family."""
    dst = template_version_dir(slug, dir_name)
    if os.path.exists(dst):
        raise FileExistsError(f"template version directory already exists: {dir_name}")
    os.makedirs(template_dir(slug), exist_ok=True)
    shutil.copytree(src_dir, dst)


def instantiate_into(slug: str, dir_name: str, dest_latest_dir: str) -> None:
    """Physical copy of a template version into a new project's `latest/` —
    never a live link, so later changes to the template (or the project)
    never propagate either direction."""
    src = template_version_dir(slug, dir_name)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"template version directory not found: {dir_name}")
    shutil.copytree(src, dest_latest_dir)
