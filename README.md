# UX-Proto

UX-Proto is agent-piloted visual prototyping. An agent writes real code — an
HTML/CSS/JS frontend plus a mock Python backend — per project, with live
hot-reload, entirely through MCP tools (no visual editor in the UI). It's a
container-tier AW workspace app: a FastAPI backend (project CRUD, static
project serving, a hot-reload WebSocket hub, and one isolated subprocess per
project's mock backend) plus a Vite/React dashboard, in one image.

## What It Does

- Scaffolds new prototype projects (frontend + mock backend) from a name or
  from a saved template version.
- Live-reloads a project's frontend on every file write; hot-reloads its
  mock backend on demand, isolated per project.
- Captures DOM/console state and screenshots of a project's live browser
  tab for an agent to inspect without a separate browser automation tool.
- Freezes immutable numbered snapshots of a project and lets a saved
  version become the one served publicly.

## Runtime Notes

- **Tier**: `container`. Image bakes the full application source in
  (backend + frontend + node_modules) — no bind mount, so it runs the same
  way wherever it's installed.
- **Postgres**: the backend needs a reachable Postgres to store the
  `projects` table (database `ux_proto`, auto-created on first connect).
  Point it at your Postgres via env vars — `UX_PROTO_DB_URL` (default
  `postgresql://postgres:postgres@127.0.0.1:5432/ux_proto`) and
  `UX_PROTO_ADMIN_DB_URL` (default
  `postgresql://postgres:postgres@127.0.0.1:5432/postgres`, used once to
  `CREATE DATABASE` if it doesn't exist yet). A workspace installing this
  app needs a Postgres companion/endpoint reachable at those addresses, or
  the env vars overridden to point at one.
- **Chromium**: the image installs its own isolated Playwright Chromium for
  the `faithful` DOM-to-image capture path (`renderer.py`) — separate from
  any shared browser app in the workspace.
- **Per-project subdomain routing**: each project's mock backend runs in an
  isolated subprocess from a port pool (30021-30120) and is normally reached
  through a per-project subdomain (`ux-proto--<slug>.app.<domain>`) so each
  prototype gets real origin isolation. That subdomain routing is an
  aw-workspace edge/Caddy concern outside this app's container and may need
  additional wiring depending on how the workspace runtime exposes
  container-tier apps — the core dashboard on the app's published port is
  what to verify first after install.

## Known limitations

- The MCP tools that return a screenshot file path (`get_dom` with
  `as_image=true`) return a path inside the container's own data volume
  (`/data/projects/...`). Translating that into a path visible to the
  calling agent depends on how aw-workspace mounts this app's data volume —
  not yet wired up for the container-tier packaging (see
  `mcp/aw_ux_proto.py`).

## Layout

- `container/` — self-contained Dockerfile + baked-in application source
  (`src/api` FastAPI backend, `src/app` Vite/React dashboard,
  `entrypoint.sh`).
- `mcp/aw_ux_proto.py` — the ported MCP server (tools: create_project,
  list_projects, write_file, read_file, inject_js, hot_reload_backend,
  get_dom, create_snapshot, and more). Declared in `mcp.json` for the MCP
  Gateway app's `scan_app_mcp_servers()`.
- `aw-app.json` — the marketplace manifest.

## Release / Build

- `.github/workflows/release.yml` calls `tekflox/aw-marketplace`'s shared
  release workflow — every push to `master` runs `tests/validate_manifest.py`
  and bumps/tags a release before opening the marketplace catalog sync PR.
- `.github/workflows/build.yml` builds `container/Dockerfile` and pushes
  `ghcr.io/tekflox/aw-app-ux-proto:latest` (+ version + SHA tags) on release
  bump commits, or on demand via `workflow_dispatch`.
