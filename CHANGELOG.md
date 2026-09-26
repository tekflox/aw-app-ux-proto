# Change History

This file records user-facing UX-Proto changes.

## Unreleased

- Fix `public_url` (returned by the API, the dashboard's external link, and
  the MCP server's `get_project_url`): it was still computing the legacy
  monolith's `ux-proto--<slug>.app.{AW_DOMAIN}` child-subdomain scheme, which
  is not routed anywhere in the decoupled-apps framework — every external
  link a project handed out was unreachable. Now builds the working
  path-based route on this app's own workspace-scoped host instead
  (`https://ux-proto.app.<workspace>.workspace.aw.tekflox.com/p/<slug>/_frame/`).

## 0.1.0

- Initial container-tier packaging of UX-Proto, ported from the
  `agentic-workspace` monolith custom app (`src/custom_apps/ux-proto`).
- Backend + frontend source baked into the image; ported MCP server shipped
  under `mcp/aw_ux_proto.py` with `mcp.json` wiring it into the MCP Gateway.
