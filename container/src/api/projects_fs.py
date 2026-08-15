"""On-disk layout for UX-Proto projects — data/ux-proto/projects/<slug>/.

Bind-mounted from the host (see aw.json docker_services volumes) so
projects survive container recreation.

    <slug>/
      meta.json
      latest/            <- the live working copy; write_file/read_file
        frontend/{index.html,style.css,app.js,...}   always target this
        backend.py
      1/                 <- immutable snapshots, made by create_snapshot()
      2/                    (full copy of `latest/` at that point in time)
      ...

Snapshots are plain integer-named sibling directories, not a separate
registry — "next version" is just (max existing int dir) + 1, and listing
is a directory scan. No DB table needed: the filesystem IS the source of
truth, same as the rest of this module.
"""

from __future__ import annotations

import json
import os
import shutil
import time

DATA_ROOT = os.environ.get("UX_PROTO_DATA_ROOT", "/data/projects")

LATEST = "latest"

_TEMPLATE_INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{name}</title>
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <h1>{name}</h1>
  <p>Novo projeto UX-Proto. Edite via MCP (write_file) para começar.</p>
  <script src="app.js"></script>
</body>
</html>
"""

_TEMPLATE_STYLE_CSS = """body {
  font-family: system-ui, sans-serif;
  margin: 0;
  padding: 2rem;
  background: #0d1117;
  color: #e6edf3;
}
"""

_TEMPLATE_APP_JS = """console.log("UX-Proto project loaded");
"""

_TEMPLATE_BACKEND_PY = '''"""Mock backend for this UX-Proto project.

Run as an isolated subprocess by BackendSupervisor — a FastAPI app on
whatever port it's assigned. Exposed to the browser at /p/<slug>/api/*.
"""

from fastapi import FastAPI

app = FastAPI()


@app.get("/api/hello")
def hello():
    return {"message": "hello from the mock backend"}
'''


def project_dir(slug: str) -> str:
    return os.path.join(DATA_ROOT, slug)


def _version_dir(slug: str, version: str = LATEST) -> str:
    return os.path.join(project_dir(slug), version)


def frontend_dir(slug: str, version: str = LATEST) -> str:
    return os.path.join(_version_dir(slug, version), "frontend")


def backend_path(slug: str, version: str = LATEST) -> str:
    return os.path.join(_version_dir(slug, version), "backend.py")


def scaffold(slug: str, name: str) -> None:
    fe = frontend_dir(slug)
    os.makedirs(fe, exist_ok=True)
    _write(os.path.join(fe, "index.html"), _TEMPLATE_INDEX_HTML.format(name=name))
    _write(os.path.join(fe, "style.css"), _TEMPLATE_STYLE_CSS)
    _write(os.path.join(fe, "app.js"), _TEMPLATE_APP_JS)
    _write(backend_path(slug), _TEMPLATE_BACKEND_PY)
    write_meta(slug, name)


def write_meta(slug: str, name: str) -> None:
    _write(
        os.path.join(project_dir(slug), "meta.json"),
        json.dumps({"name": name, "slug": slug, "created_at": time.time(), "schema_version": 2}, indent=2),
    )


def version_dir(slug: str, version: str = LATEST) -> str:
    """Public wrapper of _version_dir — the project's `latest/` (or a numbered
    snapshot) directory, used by templates_fs to copy to/from a project."""
    return _version_dir(slug, version)


def _write(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)


def read_file(slug: str, rel_path: str) -> str:
    base = frontend_dir(slug) if rel_path != "backend.py" else _version_dir(slug)
    full = _safe_join(base, rel_path)
    with open(full) as f:
        return f.read()


def write_file(slug: str, rel_path: str, content: str) -> None:
    base = frontend_dir(slug) if rel_path != "backend.py" else _version_dir(slug)
    full = _safe_join(base, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


def list_snapshots(slug: str) -> list[dict]:
    """Integer-named sibling directories of latest/, newest first."""
    pdir = project_dir(slug)
    if not os.path.isdir(pdir):
        return []
    out = []
    for entry in os.listdir(pdir):
        if not entry.isdigit():
            continue
        full = os.path.join(pdir, entry)
        if os.path.isdir(full):
            out.append({"version": int(entry), "created_at": os.path.getmtime(full)})
    out.sort(key=lambda s: s["version"], reverse=True)
    return out


def create_snapshot(slug: str) -> dict:
    """Copy latest/ into the next integer-named sibling directory."""
    existing = [s["version"] for s in list_snapshots(slug)]
    next_version = (max(existing) + 1) if existing else 1
    src = _version_dir(slug)
    dst = _version_dir(slug, str(next_version))
    shutil.copytree(src, dst)
    return {"version": next_version, "created_at": os.path.getmtime(dst)}


def version_exists(slug: str, version: str) -> bool:
    if version == LATEST:
        return os.path.isdir(_version_dir(slug))
    return version.isdigit() and os.path.isdir(_version_dir(slug, version))


def _safe_join(base: str, rel_path: str) -> str:
    """Reject path traversal — rel_path must resolve to stay under base."""
    full = os.path.normpath(os.path.join(base, rel_path))
    base_norm = os.path.normpath(base)
    if full != base_norm and not full.startswith(base_norm + os.sep):
        raise ValueError(f"path escapes project directory: {rel_path}")
    return full
