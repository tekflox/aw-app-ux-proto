# App subdomains (and per-project child subdomains)

How an installed app is reachable at its own hostname inside an
aw-workspace, how a request travels from the browser to the app
container, and how an app can additionally serve a **family of per-entity
child subdomains** (e.g. UX-Proto's `ux-proto--<project>.app.<ws>…`).

This documents the *reused* wildcard that already exists at the edge, the
in-workspace routing that consumes it, and the internal logic an app must
implement to make sense of a child host. It mirrors the monolith's
long-standing `wildcard_children` mechanism (see
`agentic-workspace/src/libs/caddy_template.py`).

## The host shape

For a workspace with slug `<ws>` (e.g. `aw`) on base domain
`workspace.aw.tekflox.com` (`src/api/workspace_url.py::base_domain`):

| Surface | Host |
|---|---|
| SPA (dashboard) | `<ws>.workspace.aw.tekflox.com` |
| Workspace API | `api.<ws>.workspace.aw.tekflox.com` |
| App (per-app) | `<app>.app.<ws>.workspace.aw.tekflox.com` |
| App child (per-entity) | `<app>--<child>.app.<ws>.workspace.aw.tekflox.com` |

The per-app URL is composed (never stored) by
`src/apps/containers.py::app_public_url`:

```python
return f"https://{app_id}.app.{slug}.{workspace_url.base_domain()}"
```

A child host is one DNS label deeper on the *same* leftmost position
(`ux-proto--teste-ux-proto` is a single label), so it is covered by the
same wildcard cert — no new ACME order.

## Layer 1 — the edge (already in place)

`aw-backend` generates a per-workspace Caddy fragment
(`workspace_caddy_template.py`) imported by the host Caddyfile as
`import /workspaces-caddy/workspaces.caddy`. The block for workspace `aw`:

```caddy
aw.workspace.aw.tekflox.com,
*.aw.workspace.aw.tekflox.com,
*.app.aw.workspace.aw.tekflox.com {
    tls { dns route53 { wait_for_route53_sync true } }   # one wildcard cert, DNS-01
    encode gzip zstd

    @spa host aw.workspace.aw.tekflox.com                 # SPA static files
    @spa_apps_api { host aw.workspace…; path /api/* /ws/apps/* }
    handle @spa_apps_api { reverse_proxy aw-sandbox:9025 }  # → tunnel
    @spa_control_ws { host aw.workspace…; path /ws/* }
    handle @spa_control_ws { reverse_proxy aw-sandbox:9025 }
    handle @spa { root * /srv/static/aw-workspace-ui; try_files {path} /index.html; file_server }

    handle { reverse_proxy aw-sandbox:9025 }              # catch-all → tunnel
}
```

The important line is the **catch-all** `handle { reverse_proxy
aw-sandbox:9025 }`: any host under `*.app.aw.workspace.aw.tekflox.com`
that is *not* the SPA host — including both `ux-proto.app.…` **and**
`ux-proto--<project>.app.…` — is tunneled unconditionally to the
workspace at `:9025` with the original `Host` header intact. **The child
subdomain therefore already reaches the workspace today.** The wildcard
TLS + routing were reserved at the F4 split; nothing at the edge needs to
change to add child subdomains.

## Layer 2 — the workspace host router

The workspace ASGI process resolves an incoming `Host` to an installed
app in `src/apps/runtime.py::AppRuntime._attach_mount`. Every app is
attached at two entry points against the *same* IdentityGuard-wrapped
ASGI app:

```python
mount      = Mount(f"/api/apps/{app_id}", app=guarded)   # path-based
host_mount = Host(f"{app_id}.app.{{_:str}}", app=guarded) # subdomain-based
self.host.router.routes.append(mount)
self.host.router.routes.append(host_mount)
```

`Host("{app_id}.app.{_:str}")` matches `‹app_id›.app.‹anything›` — the
`{_:str}` capture greedily swallows the rest of the hostname (workspace
slug + base domain) because Starlette's default `str` convertor does not
exclude `.`. This is shared by Tier-1 (in-process) and Tier-2 (container,
via `ContainerReverseProxy`) apps — both call `_attach_mount`, which is
why e.g. `crispal.app.<ws>…` resolves to the crispal container.

### The gap for child subdomains

`Host("{app_id}.app.{_:str}")` matches the **exact** leftmost label
`app_id`. A child host `ux-proto--teste.app.…` has leftmost label
`ux-proto--teste`, which does **not** match — so today the child
subdomain reaches the workspace (Layer 1) but has no route and 404s /
falls through. This is the single missing piece.

### The fix — an opt-in child host mount

When an app opts into child subdomains, `_attach_mount` appends a second
`Host` route that maps the child family to the *same* guarded app:

```python
# gated on the app opting in (manifest flag, see below)
child_mount = Host(f"{app_id}--{{_child:str}}.app.{{_:str}}", app=guarded)
self.host.router.routes.append(child_mount)
loaded.child_host_mount = child_mount   # tracked for unmount, like host_mount
```

`Host("ux-proto--{_child:str}.app.{_:str}")` matches
`ux-proto--‹child›.app.‹rest›` and hands the request to the app
unchanged (original `Host` preserved). The workspace does **not** try to
interpret `‹child›` — that is the app's job (Layer 3). This mirrors the
monolith exactly, where Caddy matches
`^‹app›--[a-zA-Z0-9-]+\.app\.‹domain›$` with `header_regexp Host` and
reverse-proxies to the same upstream port.

## Layer 3 — the app's internal logic

The platform delivers a child request to the app with the original
`Host`; the app must:

1. **Redirect the bare child root to its content route.** In the
   monolith, Caddy issues `redir @root /_frame/ 302` (a real redirect, so
   the browser address bar becomes `/_frame/` and the prototype's
   relative asset links resolve correctly instead of falling through to
   the SPA). The workspace edge does **not** do this rewrite, so the app
   must: on a child host with path `/`, 302-redirect to its root path
   (UX-Proto: `/_frame/`). UX-Proto does this in a tiny Vite middleware
   (`vite.config.js`) because Vite owns `/`; the backend owns `/_frame*`.

2. **Derive the entity from the `Host` header.** UX-Proto
   (`src/api/main.py`):

   ```python
   _CHILD_HOST_PREFIX = "ux-proto--"
   def _slug_from_host(host):
       label = host.split(":")[0].split(".")[0]
       return label[len(_CHILD_HOST_PREFIX):] if label.startswith(_CHILD_HOST_PREFIX) else None
   ```

   Its bare `/_frame*` routes call `_require_host_slug(request)` and then
   serve/proxy that project, sharing the slug-parameterized core with the
   path-based `/p/<slug>/_frame*` routes.

3. **Keep WebSockets slug-in-path.** `/ws/<slug>` is a path parameter, so
   it is reached identically from the child origin and the path origin;
   the injected client derives the slug from the hostname on child pages.

## CORS — permissive, on purpose

The child-subdomain design makes each project a **distinct origin**, so
the dashboard shell (`<app>.app…`), the child (`<app>--<slug>.app…`), and
the path-based iframe (`/api/apps/<app>/p/<slug>/_frame`) are three
different origins hitting the same backend. The app therefore runs fully
open CORS (UX-Proto `src/api/main.py`, copied from the monolith):

```python
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
```

The edge stays out of CORS entirely (never injects `Access-Control-*`,
never gates `OPTIONS`) so anonymous preflights reach the app's own
handler — matching the monolith (`caddy_template.py`). **CORS must remain
disabled/permissive in UX-Proto** for the cross-origin iframe + WS to
work.

## Iframe embedding (X-Frame-Options / CSP)

For an app (or child) to render inside the dashboard iframe, framing
headers must allow it. The monolith strips them at the proxy
(`header_down -X-Frame-Options` + rewrite `frame-ancestors 'none'` →
`frame-ancestors https://<domain>`). The workspace equivalent lives in
the app proxy / edge; a child host reuses the same block. Apps should not
emit `X-Frame-Options: DENY` or a `frame-ancestors 'none'` CSP.

## How an app opts in

1. **Manifest flag** — declare child subdomains in `aw-app.json`
   (mirrors the monolith's `"wildcard_children": true`). The workspace
   reads this in `_attach_mount` and adds the `child_mount` above. The
   child label separator is `--` and the prefix is the app `id`
   (`<id>--<child>`).
2. **Backend host logic** — implement `Host`-based entity resolution
   (`_slug_from_host` pattern) and a bare-`/`→content redirect on child
   hosts.
3. **Correct external URLs** — the app should emit child URLs against the
   *workspace* domain. Prefer deriving the domain from the incoming
   request `Host` over a static `AW_DOMAIN` env, so the same image is
   correct in every workspace (the SPA already derives the child origin
   from `window.location.hostname`).

## End-to-end request flow (UX-Proto child)

```
Browser  ── https://ux-proto--teste.app.aw.workspace.aw.tekflox.com/
  │
  ▼  (DNS *.app.aw.workspace… → host, wildcard TLS)
aw-caddy  ── workspaces.caddy: catch-all handle { reverse_proxy aw-sandbox:9025 }
  │            (Host header preserved)
  ▼
aw-backend :9025  ── BYOD tunnel → workspace ASGI
  │
  ▼
runtime.py Host("ux-proto--{_child}.app.{_}")  → IdentityGuard → app
  │
  ▼
UX-Proto container :10021 (Vite)
  │   • bare "/"  → 302 /_frame/            (Vite middleware, child host only)
  │   • /_frame*  → backend, _slug_from_host("ux-proto--teste…") = "teste"
  ▼
project "teste" served, /ws/teste live-reload socket
```
