"""MCP server for the aw-app-ux-proto container-tier app — agent-piloted
visual prototyping.

Every tool takes `project` (the slug) except create_project/list_projects/
get_status. stdio MCP server — talks to the ux-proto app's own FastAPI
backend, ported as-is from the monolith custom app
(src/custom_apps/ux-proto/src/api/main.py).

Runtime-endpoint adaptation from the monolith version: the monolith's MCP
process shared aw-sandbox's network namespace with the custom app container
and could reach the FastAPI backend directly on 127.0.0.1:20021 (bypassing
Vite). This packaged container-tier app runs as its own sidecar container
(named `aw-app-ux-proto`, following the aw-app-browser convention — see that
app's mcp.json/cdp_proxy.py) with only the Vite frontend port (10021)
published; the backend's internal port is not reachable from outside the
container. So this MCP targets the container's published port and relies on
Vite's own `/api` proxy (see container/src/app/vite.config.js) to reach the
backend — the REST paths and response handling below are otherwise
unchanged from the monolith version.

See Kanban card 39e5bf3b-9510-81a6-a72b-d5810f5f6796 (target
system-investigations) for the closed spec this implements.
"""

import json
import sys
import urllib.parse
import urllib.request

UX_PROTO_APP_URL = "http://aw-app-ux-proto:10021"

# The ux-proto container sees its data volume at /data/projects
# (projects_fs.py's DATA_ROOT). In the monolith, the calling MCP process
# shared a bind mount with the custom app container and could translate
# that path to a real host-visible path
# (/opt/agentic-workspace/data/ux-proto/projects). In this packaged
# container-tier app, there is no such shared bind mount by default — the
# data volume lives wherever the aw-workspace runtime places this app's
# volume, which is not yet a known constant here. Screenshot/as_image paths
# returned by get_dom therefore stay container-internal until the
# aw-workspace container-tier volume convention is wired up; see this repo's
# README "Known limitations" section.
_CONTAINER_DATA_ROOT = "/data/projects"
_SANDBOX_DATA_ROOT = "/data/projects"


def _to_sandbox_path(container_path: str) -> str:
    if container_path.startswith(_CONTAINER_DATA_ROOT):
        return _SANDBOX_DATA_ROOT + container_path[len(_CONTAINER_DATA_ROOT):]
    return container_path


def _api(method, path, body=None):
    url = f"{UX_PROTO_APP_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


_TOOLS = [
    {
        "name": "create_project",
        "description": (
            "Create a new UX-Proto project — generates a unique slug, scaffolds "
            "frontend/index.html + style.css + app.js and a mock backend.py under "
            "data/ux-proto/projects/<slug>/, and registers it in Postgres."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Display name for the project."}},
            "required": ["name"],
        },
    },
    {
        "name": "create_project_from_template",
        "description": (
            "Create a new UX-Proto project instantiated (physical copy, never a "
            "live link) from a specific version of a template — use list_templates "
            "/ list_template_versions to find slug + version_label first. Editing "
            "the template later never affects this project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name for the new project."},
                "template_slug": {"type": "string", "description": "Template family slug, e.g. 'feed-instagram'."},
                "version_label": {"type": "string", "description": "Which version of that template to copy, e.g. 'v1'."},
            },
            "required": ["name", "template_slug", "version_label"],
        },
    },
    {
        "name": "promote_to_template",
        "description": (
            "Turn a project's current working copy (latest/) into a new named "
            "template version — a physical copy, never a live link. Always adds "
            "a new version, never overwrites an existing one (409 if version_label "
            "is already taken for this template). If template_slug has never been "
            "used before, the template family is created implicitly in the same call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Source project slug to promote from."},
                "template_slug": {"type": "string", "description": "Template family slug — created if it doesn't exist yet."},
                "version_label": {"type": "string", "description": "Free-form label for the new version, e.g. 'v1', 'v2'. Must not already exist for this template."},
                "description": {"type": "string", "description": "Optional description, only used if this call creates the template family."},
            },
            "required": ["project", "template_slug", "version_label"],
        },
    },
    {
        "name": "list_templates",
        "description": "List template families available to start a new project from.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_template_versions",
        "description": "List a template's named versions, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"template_slug": {"type": "string", "description": "Template family slug."}},
            "required": ["template_slug"],
        },
    },
    {
        "name": "list_projects",
        "description": "List UX-Proto projects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_deleted": {"type": "boolean", "description": "Include soft-deleted projects. Default false."},
            },
        },
    },
    {
        "name": "soft_delete_project",
        "description": "Soft-delete a project — hides it from the dashboard, keeps it on disk (restore_project brings it back).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "restore_project",
        "description": "Restore a soft-deleted project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write/overwrite a file in a project. path is relative — one of the "
            "frontend files (index.html, style.css, app.js, or any new path under "
            "frontend/) or exactly 'backend.py' for the mock backend. Writing a "
            "frontend file pushes a live reload to every open browser tab of this "
            "project; writing backend.py does NOT auto-restart it — call "
            "hot_reload_backend after."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "path": {"type": "string", "description": "Relative file path, e.g. 'index.html' or 'backend.py'."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["project", "path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a project's current file content before editing it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "path": {"type": "string", "description": "Relative file path, e.g. 'index.html' or 'backend.py'."},
            },
            "required": ["project", "path"],
        },
    },
    {
        "name": "inject_js",
        "description": (
            "Run a JS snippet immediately in every open browser tab of this "
            "project, over the hot-reload WebSocket — no file save, no reload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "code": {"type": "string", "description": "JavaScript to eval() in the project's iframe."},
            },
            "required": ["project", "code"],
        },
    },
    {
        "name": "hot_reload_backend",
        "description": (
            "Restart this project's backend.py in isolation (its own subprocess — "
            "other projects are unaffected even if it crashes). Call after "
            "write_file('backend.py', ...)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "get_project_url",
        "description": "Get the public full-screen URL for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "get_status",
        "description": "Which projects have browser(s) connected right now, and how many.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_connections",
        "description": (
            "List the actual browser tabs/devices currently connected to one "
            "project — user agent string and how long each has been connected. "
            "Only what the WebSocket handshake already carries (User-Agent "
            "header), no extra fingerprinting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "eval_js",
        "description": (
            "Run a JS snippet in one open browser tab of this project and wait for "
            "its actual return value (or thrown error) — unlike inject_js this is "
            "request/response, not fire-and-forget. Requires at least one tab open "
            "(check get_status first). Return value is JSON-stringified where possible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "code": {"type": "string", "description": "JavaScript expression to evaluate in the project's page."},
                "timeout_seconds": {"type": "number", "description": "How long to wait for the browser to respond. Default 10."},
            },
            "required": ["project", "code"],
        },
    },
    {
        "name": "get_dom",
        "description": (
            "Read the current live DOM (outerHTML) of an element in one open "
            "browser tab of this project — lets you inspect what's actually "
            "rendered right now (post-JS mutations) without a separate browser "
            "automation tool. Requires at least one tab open. With "
            "as_image=true, instead renders that element to a PNG and returns "
            "a file path to view it. Two engines: html2canvas (default, "
            "fast) is an approximation — cross-origin images without CORS, "
            "object-fit, and some canvas/SVG edge cases won't be pixel "
            "perfect. faithful=true instead captures the live tab's actual "
            "DOM+scroll/viewport state and hands it to a real, dedicated "
            "headless Chromium (this app's own, isolated — not the shared "
            "aw-browser) to render — exact CSS fidelity, no CORS "
            "restriction, but slower and needs Chromium installed in this "
            "app's container."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "selector": {"type": "string", "description": "CSS selector for the root element to dump/capture. Default 'html' for HTML, 'body' for as_image. Ignored when faithful=true (always captures the whole page)."},
                "as_image": {"type": "boolean", "description": "Render to a PNG file instead of returning HTML text. Default false."},
                "viewport_only": {"type": "boolean", "description": "Only used with as_image=true. Crop to exactly what's currently visible/scrolled-to in the tab, instead of the element's whole scrollable extent. Default false (full extent)."},
                "faithful": {"type": "boolean", "description": "Only used with as_image=true. Use the real-Chromium engine instead of html2canvas. Default false."},
                "timeout_seconds": {"type": "number", "description": "Only used with as_image=true. Default 20."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "create_snapshot",
        "description": (
            "Freeze the project's current working copy (latest/) as a new "
            "immutable numbered version — future edits to latest keep going, "
            "this snapshot never changes. View it at the project's URL with "
            "'/_frame/v/<N>/' appended (or the UI's version picker)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "list_snapshots",
        "description": "List a project's saved snapshot versions, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug."}},
            "required": ["project"],
        },
    },
    {
        "name": "select_version",
        "description": (
            "Pick which version (latest, or a snapshot number) this project's "
            "public_url points at — persisted, so it stays selected until "
            "changed again (also settable from the dashboard's version picker; "
            "both write to the same place)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "version": {"type": "string", "description": "'latest' or a snapshot number (as a string), e.g. '3'."},
            },
            "required": ["project", "version"],
        },
    },
    {
        "name": "get_console_logs",
        "description": (
            "Read recent console.log/info/warn/error output (and uncaught JS "
            "errors) from this project's open browser tab(s) — relayed live over "
            "the hot-reload WebSocket, buffered server-side (last 300 entries)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug."},
                "limit": {"type": "integer", "description": "Max entries to return, most recent. Default 100."},
            },
            "required": ["project"],
        },
    },
]


def handle_request(request: dict):
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aw-ux-proto", "version": "1.0.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}}

    if method == "tools/call":
        name = request.get("params", {}).get("name", "")
        args = request.get("params", {}).get("arguments", {}) or {}

        if name == "create_project":
            r = _api("POST", "/api/projects", {"name": args["name"]})
            if r.get("slug"):
                return _ok(req_id, f"Created project '{r['name']}' (slug={r['slug']}). URL: {r['public_url']}")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "create_project_from_template":
            r = _api(
                "POST",
                "/api/projects",
                {"name": args["name"], "template_slug": args["template_slug"], "version_label": args["version_label"]},
            )
            if r.get("slug"):
                return _ok(req_id, f"Created project '{r['name']}' (slug={r['slug']}) from {args['template_slug']}@{args['version_label']}. URL: {r['public_url']}")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "promote_to_template":
            body = {
                "project": args["project"],
                "template_slug": args["template_slug"],
                "version_label": args["version_label"],
            }
            if "description" in args:
                body["description"] = args["description"]
            r = _api("POST", "/api/templates", body)
            if r.get("version_label"):
                return _ok(req_id, f"Promoted '{args['project']}' to template {r['template_slug']}@{r['version_label']}.")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "list_templates":
            r = _api("GET", "/api/templates")
            templates = r.get("templates")
            if templates is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not templates:
                return _ok(req_id, "No templates yet — promote_to_template from a project to create one.")
            lines = [f"- {t['slug']} ({t['name']})" + (f": {t['description']}" if t.get("description") else "") for t in templates]
            return _ok(req_id, "\n".join(lines))

        if name == "list_template_versions":
            r = _api("GET", f"/api/templates/{args['template_slug']}/versions")
            versions = r.get("versions")
            if versions is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not versions:
                return _ok(req_id, f"Template '{args['template_slug']}' has no versions yet.")
            lines = [f"- {v['version_label']} (created {v['created_at']})" for v in versions]
            return _ok(req_id, "\n".join(lines))

        if name == "list_projects":
            include_deleted = args.get("include_deleted", False)
            r = _api("GET", f"/api/projects?include_deleted={'true' if include_deleted else 'false'}")
            projects = r.get("projects")
            if projects is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not projects:
                return _ok(req_id, "No projects yet.")
            lines = [
                f"- {p['slug']} ({p['name']}, {p['status']}, {p['connected']} connected): {p['public_url']}"
                for p in projects
            ]
            return _ok(req_id, "\n".join(lines))

        if name == "soft_delete_project":
            r = _api("DELETE", f"/api/projects/{args['project']}")
            if r.get("slug"):
                return _ok(req_id, f"Soft-deleted '{r['slug']}'.")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "restore_project":
            r = _api("POST", f"/api/projects/{args['project']}/restore")
            if r.get("slug"):
                return _ok(req_id, f"Restored '{r['slug']}'.")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "write_file":
            r = _api(
                "PUT",
                f"/api/projects/{args['project']}/file",
                {"path": args["path"], "content": args["content"]},
            )
            if r.get("ok"):
                return _ok(req_id, f"Wrote {args['path']} to '{args['project']}'. Open tabs reloaded.")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "read_file":
            r = _api("GET", f"/api/projects/{args['project']}/file?path={urllib.parse.quote(args['path'])}")
            if "content" in r:
                return _ok(req_id, r["content"])
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "inject_js":
            r = _api("POST", f"/api/projects/{args['project']}/inject_js", {"code": args["code"]})
            if r.get("ok"):
                return _ok(req_id, f"Injected JS into {r['recipients']} open tab(s) of '{args['project']}'.")
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "hot_reload_backend":
            r = _api("POST", f"/api/projects/{args['project']}/hot_reload_backend")
            if r.get("ok"):
                return _ok(req_id, f"backend.py reloaded on port {r['port']}.")
            return _err(req_id, f"backend.py failed to reload: {r.get('error', 'unknown error')}")

        if name == "get_project_url":
            r = _api("GET", f"/api/projects/{args['project']}")
            if r.get("public_url"):
                return _ok(req_id, r["public_url"])
            return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")

        if name == "get_status":
            r = _api("GET", "/api/status")
            projects = r.get("projects")
            if projects is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not projects:
                return _ok(req_id, "No projects have a browser connected right now.")
            lines = [f"- {slug}: {info['connected']} connected" for slug, info in projects.items()]
            return _ok(req_id, "\n".join(lines))

        if name == "list_connections":
            r = _api("GET", f"/api/projects/{args['project']}/connections")
            conns = r.get("connections")
            if conns is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not conns:
                return _ok(req_id, "No browser tabs connected right now.")
            lines = []
            for c in conns:
                secs = c.get("connected_for_seconds")
                dur = f"{secs}s" if secs is not None else "unknown duration"
                lines.append(f"- {c['user_agent']} (connected {dur})")
            return _ok(req_id, "\n".join(lines))

        if name == "create_snapshot":
            r = _api("POST", f"/api/projects/{args['project']}/snapshots")
            if "version" not in r:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            return _ok(req_id, f"Created snapshot {r['version']} of '{args['project']}'.")

        if name == "list_snapshots":
            r = _api("GET", f"/api/projects/{args['project']}/snapshots")
            snaps = r.get("snapshots")
            if snaps is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not snaps:
                return _ok(req_id, "No snapshots yet — create_snapshot to freeze the current version.")
            lines = [f"- {s['version']}" for s in snaps]
            return _ok(req_id, "\n".join(lines))

        if name == "select_version":
            r = _api("PUT", f"/api/projects/{args['project']}/selected_version", {"version": args["version"]})
            if not r.get("slug"):
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            return _ok(req_id, f"'{args['project']}' now points at version {r['selected_version']}. URL: {r['public_url']}")

        if name == "eval_js":
            body = {"code": args["code"]}
            if "timeout_seconds" in args:
                body["timeout_seconds"] = args["timeout_seconds"]
            r = _api("POST", f"/api/projects/{args['project']}/eval_js", body)
            if "ok" not in r:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not r["ok"]:
                return _err(req_id, f"JS threw: {r.get('error')}")
            return _ok(req_id, r.get("result") if r.get("result") is not None else "undefined")

        if name == "get_dom" and not args.get("as_image"):
            selector = urllib.parse.quote(args.get("selector", "html"))
            r = _api("GET", f"/api/projects/{args['project']}/dom?selector={selector}")
            if "html" not in r:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            return _ok(req_id, r["html"])

        if name == "get_dom" and args.get("as_image"):
            body = {"selector": args["selector"]} if "selector" in args else {}
            if "timeout_seconds" in args:
                body["timeout_seconds"] = args["timeout_seconds"]
            if "viewport_only" in args:
                body["viewport_only"] = args["viewport_only"]
            if "faithful" in args:
                body["faithful"] = args["faithful"]
            r = _api("POST", f"/api/projects/{args['project']}/screenshot", body)
            if not r.get("ok"):
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            return _ok(req_id, _to_sandbox_path(r["path"]))

        if name == "get_console_logs":
            limit = args.get("limit", 100)
            r = _api("GET", f"/api/projects/{args['project']}/console?limit={limit}")
            logs = r.get("logs")
            if logs is None:
                return _err(req_id, f"Error: {r.get('detail') or r.get('error', 'unknown')}")
            if not logs:
                return _ok(req_id, "No console output captured yet — is a browser tab open on this project?")
            lines = [f"[{entry['level']}] {' '.join(entry['args'])}" for entry in logs]
            return _ok(req_id, "\n".join(lines))

        return _err(req_id, f"Unknown tool: {name}")

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def _ok(req_id, text):
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def _err(req_id, text):
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
