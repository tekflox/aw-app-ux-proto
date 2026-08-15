"""FastAPI backend for UX-Proto — agent-piloted visual prototyping.

Serves three things on one port (127.0.0.1:20021 inside the shared
aw-sandbox netns, proxied by Vite under /api — see vite.config.js):

- REST `/api/projects*` — CRUD for the Postgres `projects` table, called by
  both the dashboard SPA and the aw-ux-proto MCP server (src/mcp/aw_ux_proto.py).
- `/p/<slug>/_frame` — the actual prototype's static files (frontend/index.html
  etc.), with a small hot-reload client injected into index.html so file
  writes and inject_js calls show up live without a manual refresh. Lives
  under `_frame` (not bare `/p/<slug>`) because the dashboard SPA owns the
  bare `/p/<slug>` route client-side and renders this as an `<iframe>` —
  same path for both would recurse (see Architect finding #2 on Kanban
  card 39e5bf3b...: the iframe `src` must resolve somewhere other than the
  shell's own route, or `allow-same-origin` later would let a broken
  prototype reach the dashboard's own DOM/localStorage).
- `/p/<slug>/_frame/api/*` — proxied through BackendSupervisor to that
  project's own isolated backend.py subprocess (mock API written by the agent).
- `/ws/<slug>` — the hot-reload hub; browsers with that project open
  connect here and receive reload/inject_js pushes.

See the Kanban card 39e5bf3b-9510-81a6-a72b-d5810f5f6796 (target
system-investigations) for the full closed spec + Architect review this
implements.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import time
from collections import deque

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import db
import projects_fs
import templates_fs
from renderer import inject_base, renderer, strip_scripts
from supervisor import BackendSupervisor

app = FastAPI(title="UX-Proto")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AW_DOMAIN = os.environ.get("AW_DOMAIN", "aw.tekflox.com")

supervisor = BackendSupervisor()

# slug -> set[WebSocket], only for browsers with that project open (/p/<slug>)
_ws_clients: dict[str, set[WebSocket]] = {}

# WebSocket -> {"user_agent", "connected_at"} — only what the handshake
# request already carries (User-Agent header), no extra client-side
# fingerprinting. Keyed by the socket object itself (not the slug) so a
# single lookup covers "which real device/browser is this connection",
# regardless of which project's room it's in.
_ws_meta: dict[WebSocket, dict] = {}

# The hub is bidirectional beyond just server->browser pushes: eval_js needs
# the browser's actual return value back (not fire-and-forget like
# inject_js), and console logging needs the browser's own console.* calls
# relayed to the agent. Both ride the same /ws/<slug> connection the
# hot-reload client already holds open — no separate channel needed.
_eval_id_counter = itertools.count(1)
_eval_futures: dict[int, asyncio.Future] = {}
_console_logs: dict[str, deque] = {}  # slug -> deque[{"level","args","ts"}], newest last
_CONSOLE_LOG_MAXLEN = 300

_screenshot_id_counter = itertools.count(1)
VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor")


@app.on_event("startup")
def _startup() -> None:
    db.init()

    async def _reaper() -> None:
        while True:
            await asyncio.sleep(60)
            supervisor.reap_idle()
            await renderer.reap_if_idle()

    asyncio.get_event_loop().create_task(_reaper())


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "ux-proto"}


# ---------------------------------------------------------------------------
# REST — projects CRUD (used by the dashboard SPA and the MCP server)
# ---------------------------------------------------------------------------


class _CreateIn(BaseModel):
    name: str
    # Both unset -> today's blank scaffold, unchanged. Both set -> instantiate
    # a physical copy of that template version instead of the blank scaffold.
    template_slug: str | None = None
    version_label: str | None = None


def _project_url(slug: str, version: str = projects_fs.LATEST) -> str:
    # Bare root on the child subdomain — Caddy rewrites "/" to "/_frame/"
    # before it ever reaches Vite (see caddy_template.py wildcard_children),
    # so a real user pasting/typing this exact URL lands on the project,
    # not the dashboard SPA's index.html. A non-latest selected_version
    # points straight at its path-based /v/<N>/ route instead — see
    # set_selected_version for why this is persisted per-project rather
    # than only ever reflecting latest.
    base = f"https://ux-proto--{slug}.app.{AW_DOMAIN}"
    return f"{base}/" if version == projects_fs.LATEST else f"{base}/_frame/v/{version}/"


def _serialize(row: dict) -> dict:
    version = row.get("selected_version") or projects_fs.LATEST
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None,
        "selected_version": version,
        "public_url": _project_url(row["slug"], version),
        "connected": len(_ws_clients.get(row["slug"], ())),
        "template_slug": row.get("template_slug"),
        "template_version_label": row.get("template_version_label"),
    }


@app.post("/api/projects")
def create_project(body: _CreateIn):
    if not body.template_slug:
        row = db.create_project(body.name)
        projects_fs.scaffold(row["slug"], row["name"])
        return _serialize(row)

    template = db.get_template(body.template_slug)
    if not template:
        raise HTTPException(404, f"template not found: {body.template_slug!r}")
    if not body.version_label:
        raise HTTPException(400, "version_label is required when template_slug is set")
    version = db.get_template_version(body.template_slug, body.version_label)
    if not version:
        raise HTTPException(
            404, f"version not found: template={body.template_slug!r} version_label={body.version_label!r}"
        )

    row = db.create_project(
        body.name,
        template_slug=template["slug"],
        template_version_label=version["version_label"],
        template_version_id=str(version["id"]),
    )
    # Physical copy (never a live link) — editing the template later never
    # affects this project, and this project editing latest/ never affects
    # the template version it started from.
    templates_fs.instantiate_into(template["slug"], version["dir_name"], projects_fs.version_dir(row["slug"]))
    projects_fs.write_meta(row["slug"], row["name"])
    return _serialize(row)


@app.get("/api/projects")
def list_projects(include_deleted: bool = False):
    return {"projects": [_serialize(r) for r in db.list_projects(include_deleted)]}


@app.get("/api/projects/{slug}")
def get_project(slug: str):
    row = db.get_project(slug)
    if not row:
        raise HTTPException(404, "project not found")
    return _serialize(row)


@app.delete("/api/projects/{slug}")
def soft_delete_project(slug: str):
    row = db.soft_delete_project(slug)
    if not row:
        raise HTTPException(404, "project not found")
    return _serialize(row)


@app.post("/api/projects/{slug}/restore")
def restore_project(slug: str):
    row = db.restore_project(slug)
    if not row:
        raise HTTPException(404, "project not found")
    return _serialize(row)


# ---------------------------------------------------------------------------
# REST — templates (agent-only creation via MCP; humans only view/pick)
# ---------------------------------------------------------------------------


def _serialize_template(t: dict) -> dict:
    return {
        "slug": t["slug"],
        "name": t["name"],
        "description": t.get("description"),
        "created_at": t["created_at"].isoformat() if t.get("created_at") else None,
    }


def _serialize_template_version(template_slug: str, v: dict) -> dict:
    return {
        "template_slug": template_slug,
        "version_label": v["version_label"],
        "created_at": v["created_at"].isoformat() if v.get("created_at") else None,
        "source_project_slug": v.get("source_project_slug"),
        "source_version": v.get("source_version"),
    }


@app.get("/api/templates")
def list_templates():
    return {"templates": [_serialize_template(t) for t in db.list_templates()]}


@app.get("/api/templates/{slug}/versions")
def list_template_versions(slug: str):
    template = db.get_template(slug)
    if not template:
        raise HTTPException(404, f"template not found: {slug!r}")
    versions = db.list_template_versions(slug)
    return {
        "template": _serialize_template(template),
        "versions": [_serialize_template_version(slug, v) for v in versions],
    }


class _PromoteTemplateIn(BaseModel):
    project: str
    template_slug: str
    version_label: str
    description: str | None = None


@app.post("/api/templates")
def promote_to_template(body: _PromoteTemplateIn):
    _require_project(body.project)
    # First promotion to a slug nobody's used yet auto-creates the family —
    # no separate "create template" step required.
    template = db.get_or_create_template(body.template_slug, description=body.description)
    if db.get_template_version(body.template_slug, body.version_label):
        raise HTTPException(
            409, f"version_label {body.version_label!r} already exists for template {body.template_slug!r}"
        )
    dir_name = templates_fs.safe_dir_name(body.version_label)
    src_dir = projects_fs.version_dir(body.project)
    try:
        templates_fs.promote_from_project(src_dir, body.template_slug, dir_name)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    try:
        version = db.insert_template_version(
            template["id"], body.version_label, dir_name,
            source_project_slug=body.project, source_version=projects_fs.LATEST,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _serialize_template_version(body.template_slug, version)


class _FileIn(BaseModel):
    path: str
    content: str


@app.get("/api/projects/{slug}/file")
async def read_file(slug: str, path: str):
    _require_project(slug)
    try:
        return {"path": path, "content": projects_fs.read_file(slug, path)}
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, str(exc))


@app.put("/api/projects/{slug}/file")
async def write_file(slug: str, body: _FileIn):
    _require_project(slug)
    try:
        projects_fs.write_file(slug, body.path, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await _broadcast(slug, {"type": "reload"})
    return {"ok": True, "path": body.path}


class _InjectIn(BaseModel):
    code: str


@app.post("/api/projects/{slug}/inject_js")
async def inject_js(slug: str, body: _InjectIn):
    _require_project(slug)
    await _broadcast(slug, {"type": "inject_js", "code": body.code})
    return {"ok": True, "recipients": len(_ws_clients.get(slug, ()))}


class _EvalIn(BaseModel):
    code: str
    timeout_seconds: float = 10.0


@app.post("/api/projects/{slug}/eval_js")
async def eval_js(slug: str, body: _EvalIn):
    # Unlike inject_js (fire-and-forget broadcast to every tab), this waits
    # for one browser to actually run the code and report back its return
    # value (JSON-stringified) or thrown error — needs a live tab, and only
    # the first one to respond wins if several are open.
    _require_project(slug)
    clients = _ws_clients.get(slug, ())
    if not clients:
        raise HTTPException(409, "no browser has this project open")
    eval_id = next(_eval_id_counter)
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _eval_futures[eval_id] = future
    await _broadcast(slug, {"type": "eval", "id": eval_id, "code": body.code})
    try:
        result = await asyncio.wait_for(future, timeout=body.timeout_seconds)
    except asyncio.TimeoutError:
        raise HTTPException(504, "no response from browser within timeout")
    finally:
        _eval_futures.pop(eval_id, None)
    return result


@app.get("/api/projects/{slug}/dom")
async def get_dom(slug: str, selector: str = "html"):
    _require_project(slug)
    escaped = selector.replace("\\", "\\\\").replace("`", "\\`")
    code = f"(function(){{var el=document.querySelector(`{escaped}`); return el ? el.outerHTML : null;}})()"
    resp = await eval_js(slug, _EvalIn(code=code))
    if not resp.get("ok"):
        raise HTTPException(422, resp.get("error", "eval failed"))
    if resp.get("result") is None:
        raise HTTPException(404, f"no element matches selector {selector!r}")
    return {"selector": selector, "html": resp["result"]}


@app.get("/api/projects/{slug}/console")
def get_console_logs(slug: str, limit: int = 100):
    _require_project(slug)
    logs = list(_console_logs.get(slug, ()))[-limit:]
    return {"logs": logs}


@app.get("/api/projects/{slug}/connections")
def list_connections(slug: str):
    _require_project(slug)
    now = time.time()
    out = []
    for ws in _ws_clients.get(slug, ()):
        meta = _ws_meta.get(ws, {})
        out.append({
            "user_agent": meta.get("user_agent", "unknown"),
            "connected_at": meta.get("connected_at"),
            "connected_for_seconds": round(now - meta["connected_at"]) if meta.get("connected_at") else None,
        })
    return {"connections": out}


def _screenshot_eval_code(selector: str, viewport_only: bool) -> str:
    escaped = selector.replace("\\", "\\\\").replace("`", "\\`")
    # viewport_only crops html2canvas's render to exactly the currently
    # visible rectangle (in document coordinates: scrollX/Y as the offset,
    # innerWidth/innerHeight as the size) instead of the element's full
    # scrollable extent — "what I'm looking at right now", not the whole
    # page stitched together.
    #
    # No explicit backgroundColor override — that used to force `null`
    # (transparent), which is invisible in most PNG viewers/Telegram's own
    # preview but renders as WHITE there. A viewport crop that runs past
    # captured content (e.g. scrolled to a spot with a gap below the last
    # element) then showed a false white gap instead of the page's real —
    # often dark — background. Let html2canvas fall back to its own
    # default: reading the element's actual computed background-color.
    #
    # useCORS: true — without it, a cross-origin image (e.g. Unsplash
    # placeholders) renders blank/black in the capture even though the real
    # browser shows it fine: html2canvas otherwise loads it same-origin-only,
    # fails silently, and leaves that spot empty. This only actually pulls
    # the real pixels in if the remote host sends back
    # `Access-Control-Allow-Origin` (Unsplash does); a host that doesn't
    # will still render blank there — no way around that without allowTaint
    # (which would fix the visual gap but then poison the canvas so
    # toDataURL() itself throws, breaking the capture entirely).
    crop = (
        "x: window.scrollX, y: window.scrollY, width: window.innerWidth, height: window.innerHeight, "
        if viewport_only
        else ""
    )
    capture_opts = f"{{{crop}useCORS: true}}"
    # Loaded on demand from our own vendored copy (not a live CDN — a
    # third-party script silently updating itself is a supply-chain risk
    # we don't need to carry just for a screenshot helper), at an
    # origin-absolute path so it resolves the same whether the current
    # page is /_frame/ or a /_frame/v/<N>/ snapshot.
    return f"""(function() {{
  function ensureLib() {{
    if (window.html2canvas) return Promise.resolve();
    return new Promise(function (resolve, reject) {{
      var s = document.createElement('script');
      s.src = location.origin + '/_frame/_vendor/html2canvas.min.js';
      s.onload = resolve;
      s.onerror = function () {{ reject(new Error('failed to load html2canvas')); }};
      document.head.appendChild(s);
    }});
  }}
  return ensureLib().then(function () {{
    var el = document.querySelector(`{escaped}`) || document.body;
    return window.html2canvas(el, {capture_opts}).then(function (canvas) {{
      return canvas.toDataURL('image/png');
    }});
  }});
}})()"""


_FAITHFUL_CAPTURE_CODE = """(function () {
  return {
    html: document.documentElement.outerHTML,
    base: location.origin + location.pathname.replace(/[^/]*$/, ''),
    width: window.innerWidth,
    height: window.innerHeight,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
  };
})()"""


async def _capture_faithful_snapshot(slug: str, timeout_seconds: float) -> dict:
    # Captures the live tab's actual rendered markup + its own viewport/
    # scroll state + location (so relative asset URLs resolve against
    # wherever it actually is — /_frame/ or a /_frame/v/<N>/ snapshot —
    # without this endpoint needing to know the version itself).
    resp = await eval_js(slug, _EvalIn(code=_FAITHFUL_CAPTURE_CODE, timeout_seconds=timeout_seconds))
    if not resp.get("ok"):
        raise HTTPException(422, f"snapshot capture failed: {resp.get('error')}")
    return json.loads(resp["result"])


class _ScreenshotIn(BaseModel):
    selector: str = "body"
    viewport_only: bool = False
    faithful: bool = False
    timeout_seconds: float = 20.0


@app.post("/api/projects/{slug}/screenshot")
async def screenshot(slug: str, body: _ScreenshotIn):
    _require_project(slug)
    if body.faithful:
        # Real Chromium paints this, not a canvas approximation — fixes
        # both html2canvas gaps in one move: object-fit (and every other
        # CSS feature) renders correctly, and cross-origin images just
        # load normally (native screenshot, not canvas.toDataURL(), so
        # there's no CORS/taint restriction to work around at all).
        snap = await _capture_faithful_snapshot(slug, body.timeout_seconds)
        html = inject_base(strip_scripts(snap["html"]), snap["base"])
        if body.viewport_only:
            png_bytes = await renderer.render(
                html, snap["width"], snap["height"], full_page=False,
                scroll_to=(snap["scrollX"], snap["scrollY"]),
            )
        else:
            png_bytes = await renderer.render(html, snap["width"], snap["height"], full_page=True)
    else:
        code = _screenshot_eval_code(body.selector, body.viewport_only)
        resp = await eval_js(slug, _EvalIn(code=code, timeout_seconds=body.timeout_seconds))
        if not resp.get("ok"):
            raise HTTPException(422, f"screenshot failed: {resp.get('error')}")
        data_url = resp.get("result") or ""
        if not data_url.startswith("data:image/png;base64,"):
            raise HTTPException(500, "unexpected screenshot result — html2canvas didn't return a PNG data URL")
        png_bytes = base64.b64decode(data_url.split(",", 1)[1])
    shots_dir = os.path.join(projects_fs.project_dir(slug), ".screenshots")
    os.makedirs(shots_dir, exist_ok=True)
    full = os.path.join(shots_dir, f"{next(_screenshot_id_counter)}.png")
    with open(full, "wb") as f:
        f.write(png_bytes)
    return {"ok": True, "path": full}


@app.get("/_frame/_vendor/html2canvas.min.js")
async def serve_html2canvas_by_host(request: Request):
    # Shared across every project/version at a fixed origin-absolute path —
    # not tied to any one project's frontend_dir, so it must be declared
    # before the /_frame/{asset_path:path} and /_frame/v/{version}/* catch-alls
    # below, or those would swallow this literal path first.
    _require_host_slug(request)
    return FileResponse(os.path.join(VENDOR_DIR, "html2canvas.min.js"), media_type="application/javascript")


@app.post("/api/projects/{slug}/hot_reload_backend")
async def hot_reload_backend(slug: str):
    _require_project(slug)
    port = supervisor.reload(slug, projects_fs.backend_path(slug))
    await _broadcast(slug, {"type": "backend_reloaded", "ok": port is not None})
    if port is None:
        return {"ok": False, "error": "backend.py failed to start — check for syntax errors"}
    return {"ok": True, "port": port}


@app.get("/api/status")
def get_status():
    return {
        "projects": {
            slug: {"connected": len(sockets), "since": None}
            for slug, sockets in _ws_clients.items()
            if sockets
        }
    }


def _require_project(slug: str) -> dict:
    row = db.get_project(slug)
    if not row or row["status"] != "active":
        raise HTTPException(404, "project not found")
    return row


# ---------------------------------------------------------------------------
# /p/<slug>/_frame — serve the actual prototype (static files + proxied
# mock API). The dashboard SPA owns bare /p/<slug> and iframes this.
#
# Two ways to reach the same content, same slug-parameterized core logic:
#   - /p/<slug>/_frame*      — path-based, used before Caddy's per-project
#                              routing exists (opaque-origin iframe, Fase 3).
#   - /_frame* (bare)        — Host-derived slug, via Caddy's
#                              ux-proto--<slug>.app.{AW_DOMAIN} host_regexp
#                              route (see caddy_template.py wildcard_children).
#                              This is what makes the per-project subdomain
#                              a real distinct origin, so the iframe can
#                              safely add allow-same-origin (Architect
#                              finding #2 on Kanban 39e5bf3b...).
# ---------------------------------------------------------------------------

_CHILD_HOST_PREFIX = "ux-proto--"

_HOT_RELOAD_CLIENT = """
<script>
(function () {
  var proto = location.protocol === "https:" ? "wss:" : "ws:";
  var parts = location.pathname.split("/");
  // Path-based (/p/<slug>/_frame/...) carries the slug in the path;
  // host-based (ux-proto--<slug>.app.{domain}, bare /_frame/...) doesn't —
  // derive it from the hostname's first label instead.
  var slug = parts[1] === "p" ? parts[2] : location.hostname.split(".")[0].replace(/^ux-proto--/, "");
  var ws = new WebSocket(proto + "//" + location.host + "/ws/" + slug);

  // Best-effort JSON-ish stringify for console args and eval results — DOM
  // nodes/functions/circular refs must never throw and break the reporting
  // channel itself, so this always falls back to String(x).
  function safeStringify(v) {
    if (typeof v === "string") return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  // Relay the page's own console output back over the same socket — this
  // is what get_console_logs reads, so an agent can see runtime errors
  // without a separate browser-automation tool attached to the tab.
  ["log", "info", "warn", "error"].forEach(function (level) {
    var orig = console[level];
    console[level] = function () {
      orig.apply(console, arguments);
      try {
        ws.send(JSON.stringify({
          type: "console", level: level,
          args: Array.prototype.map.call(arguments, safeStringify),
        }));
      } catch (e) {}
    };
  });
  window.addEventListener("error", function (ev) {
    try {
      ws.send(JSON.stringify({ type: "console", level: "error", args: [String(ev.message)] }));
    } catch (e) {}
  });

  ws.onmessage = function (ev) {
    var msg = JSON.parse(ev.data);
    if (msg.type === "reload") location.reload();
    if (msg.type === "inject_js") { try { eval(msg.code); } catch (e) { console.error("inject_js error", e); } }
    if (msg.type === "backend_reloaded" && !msg.ok) console.warn("UX-Proto: backend.py failed to reload");
    if (msg.type === "eval") {
      // Request/response pair with eval_js's HTTP caller (via main.py's
      // _eval_futures) — unlike inject_js this always answers back, ok or
      // not, so the agent gets the browser's actual return value or error
      // instead of firing blind. Wrapped in Promise.resolve().then() so
      // async code (e.g. html2canvas for the screenshot tool) is awaited
      // before answering, not just its immediately-returned pending Promise.
      var payload = { type: "eval_result", id: msg.id };
      Promise.resolve().then(function () { return eval(msg.code); }).then(function (result) {
        payload.ok = true;
        payload.result = safeStringify(result);
        try { ws.send(JSON.stringify(payload)); } catch (e) {}
      }, function (e) {
        payload.ok = false;
        payload.error = String(e);
        try { ws.send(JSON.stringify(payload)); } catch (e) {}
      });
    }
  };
})();
</script>
"""


def _slug_from_host(host: str | None) -> str | None:
    """ux-proto--<slug>.app.{AW_DOMAIN} -> <slug>. None if not a child host."""
    if not host:
        return None
    label = host.split(":")[0].split(".")[0]
    if not label.startswith(_CHILD_HOST_PREFIX):
        return None
    return label[len(_CHILD_HOST_PREFIX):]


def _require_host_slug(request: Request) -> str:
    slug = _slug_from_host(request.headers.get("host"))
    if not slug:
        raise HTTPException(404, "not a ux-proto project subdomain")
    return slug


def _safe_headers(headers: dict) -> dict:
    drop = {"content-length", "transfer-encoding", "connection"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _check_version(slug: str, version: str) -> None:
    # A picked snapshot that doesn't exist 404s rather than silently
    # falling back to latest — showing different content than what was
    # asked for would be a worse bug than a loud 404.
    if not projects_fs.version_exists(slug, version):
        raise HTTPException(404, f"snapshot {version!r} not found for this project")


async def _do_proxy_project_backend(slug: str, version: str, subpath: str, request: Request) -> Response:
    _require_project(slug)
    # Each version gets its own isolated backend subprocess — a composite
    # key (not just slug) so viewing snapshot 2's mock API can't collide
    # with (or get killed by a reload of) latest's, or another snapshot's.
    supervisor_key = slug if version == projects_fs.LATEST else f"{slug}@{version}"
    status, body, headers = await supervisor.proxy(supervisor_key, projects_fs.backend_path(slug, version), subpath, request)
    return Response(content=body, status_code=status, headers=_safe_headers(headers))


def _do_serve_project_index(slug: str, version: str) -> HTMLResponse:
    _require_project(slug)
    full = os.path.join(projects_fs.frontend_dir(slug, version), "index.html")
    if not os.path.exists(full):
        raise HTTPException(404, "index.html not found for this project")
    with open(full) as f:
        html = f.read()
    if "</body>" in html:
        html = html.replace("</body>", _HOT_RELOAD_CLIENT + "</body>")
    else:
        html += _HOT_RELOAD_CLIENT
    return HTMLResponse(html)


def _do_serve_project_asset(slug: str, version: str, asset_path: str) -> FileResponse:
    _require_project(slug)
    try:
        full = projects_fs._safe_join(projects_fs.frontend_dir(slug, version), asset_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not os.path.isfile(full):
        raise HTTPException(404, "asset not found")
    return FileResponse(full)


@app.post("/api/projects/{slug}/snapshots")
def create_snapshot(slug: str):
    _require_project(slug)
    return projects_fs.create_snapshot(slug)


@app.get("/api/projects/{slug}/snapshots")
def list_snapshots(slug: str):
    _require_project(slug)
    return {"snapshots": projects_fs.list_snapshots(slug)}


class _SelectedVersionIn(BaseModel):
    version: str


@app.put("/api/projects/{slug}/selected_version")
def select_version(slug: str, body: _SelectedVersionIn):
    # Persisted (not just an in-memory/UI-only pick) so the project's
    # public_url — used from the dashboard's version picker AND by anyone
    # opening the external full-screen URL directly — points at the same
    # version until changed again, surviving a page reload or a fresh
    # dashboard session.
    _require_project(slug)
    if not projects_fs.version_exists(slug, body.version):
        raise HTTPException(404, f"snapshot {body.version!r} not found for this project")
    row = db.set_selected_version(slug, body.version)
    return _serialize(row)


# Version selection lives in the PATH (/_frame/v/<N>/...), not a query
# string — a relative asset link (style.css) in the served HTML resolves
# against the browser's current path only, dropping any query string, so
# "?v=2" would silently 404/fall back to latest for every asset the page
# loads (same bug class as the wildcard_children root-redirect fix earlier
# — see caddy_template.py). "/v/<N>/" as a real path segment means style.css
# naturally resolves to ".../v/<N>/style.css".

# --- path-based (/p/<slug>/_frame*, /p/<slug>/_frame/v/<N>/*) ---


@app.api_route("/p/{slug}/_frame/api/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_project_backend(slug: str, subpath: str, request: Request):
    return await _do_proxy_project_backend(slug, projects_fs.LATEST, subpath, request)


@app.get("/p/{slug}/_frame/")
@app.get("/p/{slug}/_frame")
async def serve_project_index(slug: str):
    return _do_serve_project_index(slug, projects_fs.LATEST)


# Versioned path-based routes MUST be declared before the generic
# /p/{slug}/_frame/{asset_path:path} catch-all below — Starlette matches
# routes in registration order, and asset_path:path would otherwise
# swallow "v/<N>/..." as its own asset_path.


@app.api_route("/p/{slug}/_frame/v/{version}/api/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_project_backend_versioned(slug: str, version: str, subpath: str, request: Request):
    _check_version(slug, version)
    return await _do_proxy_project_backend(slug, version, subpath, request)


@app.get("/p/{slug}/_frame/v/{version}")
async def serve_project_index_versioned_no_slash(slug: str, version: str):
    return RedirectResponse(f"/p/{slug}/_frame/v/{version}/")


@app.get("/p/{slug}/_frame/v/{version}/")
async def serve_project_index_versioned(slug: str, version: str):
    _check_version(slug, version)
    return _do_serve_project_index(slug, version)


@app.get("/p/{slug}/_frame/v/{version}/{asset_path:path}")
async def serve_project_asset_versioned(slug: str, version: str, asset_path: str):
    _check_version(slug, version)
    return _do_serve_project_asset(slug, version, asset_path)


@app.get("/p/{slug}/_frame/{asset_path:path}")
async def serve_project_asset(slug: str, asset_path: str):
    return _do_serve_project_asset(slug, projects_fs.LATEST, asset_path)


# --- host-based (ux-proto--<slug>.app.{AW_DOMAIN}, bare /_frame*, /_frame/v/<N>/*) ---
# Declaration order matters: the specific /api/ and /v/<N>/ routes must
# come before the catch-all {asset_path:path} route below them.


@app.api_route("/_frame/api/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_project_backend_by_host(subpath: str, request: Request):
    slug = _require_host_slug(request)
    return await _do_proxy_project_backend(slug, projects_fs.LATEST, subpath, request)


@app.get("/_frame/")
@app.get("/_frame")
async def serve_project_index_by_host(request: Request):
    slug = _require_host_slug(request)
    return _do_serve_project_index(slug, projects_fs.LATEST)


@app.api_route("/_frame/v/{version}/api/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_project_backend_versioned_by_host(version: str, subpath: str, request: Request):
    slug = _require_host_slug(request)
    _check_version(slug, version)
    return await _do_proxy_project_backend(slug, version, subpath, request)


@app.get("/_frame/v/{version}")
async def serve_project_index_versioned_by_host_no_slash(version: str, request: Request):
    return RedirectResponse(f"/_frame/v/{version}/")


@app.get("/_frame/v/{version}/")
async def serve_project_index_versioned_by_host(version: str, request: Request):
    slug = _require_host_slug(request)
    _check_version(slug, version)
    return _do_serve_project_index(slug, version)


@app.get("/_frame/v/{version}/{asset_path:path}")
async def serve_project_asset_versioned_by_host(version: str, asset_path: str, request: Request):
    slug = _require_host_slug(request)
    _check_version(slug, version)
    return _do_serve_project_asset(slug, version, asset_path)


@app.get("/_frame/{asset_path:path}")
async def serve_project_asset_by_host(asset_path: str, request: Request):
    slug = _require_host_slug(request)
    return _do_serve_project_asset(slug, projects_fs.LATEST, asset_path)


# ---------------------------------------------------------------------------
# WebSocket hub
# ---------------------------------------------------------------------------


async def _broadcast(slug: str, message: dict) -> None:
    dead = []
    for ws in _ws_clients.get(slug, ()):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.get(slug, set()).discard(ws)


@app.websocket("/ws/{slug}")
async def ws_project(websocket: WebSocket, slug: str):
    await websocket.accept()
    _ws_clients.setdefault(slug, set()).add(websocket)
    _ws_meta[websocket] = {
        "user_agent": websocket.headers.get("user-agent", "unknown"),
        "connected_at": time.time(),
    }
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "console":
                buf = _console_logs.setdefault(slug, deque(maxlen=_CONSOLE_LOG_MAXLEN))
                buf.append({"level": msg.get("level", "log"), "args": msg.get("args", [])})
            elif msg.get("type") == "eval_result":
                future = _eval_futures.get(msg.get("id"))
                if future is not None and not future.done():
                    future.set_result({"ok": msg.get("ok", False), "result": msg.get("result"), "error": msg.get("error")})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.get(slug, set()).discard(websocket)
        _ws_meta.pop(websocket, None)
