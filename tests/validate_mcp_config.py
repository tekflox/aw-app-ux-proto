#!/usr/bin/env python3
"""Validates mcp.json — the file the MCP Gateway app's
``scan_app_mcp_servers()`` reads from this app's root directory (same
mcpServers shape as the in-repo project's .mcp.json: command/args/env/type).

Run with: python3 tests/validate_mcp_config.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

config = json.loads((ROOT / "mcp.json").read_text())

assert "mcpServers" in config, "mcp.json must have a top-level 'mcpServers' object"
servers = config["mcpServers"]
assert "aw-ux-proto" in servers, "expected an 'aw-ux-proto' server entry"

server = servers["aw-ux-proto"]
assert server.get("type", "stdio") == "stdio", "aw-ux-proto server must be stdio (spawned by the gateway container)"
assert server.get("command"), "aw-ux-proto server needs a 'command'"
args = server.get("args", [])
assert args, "aw-ux-proto server needs 'args' pointing at the ported MCP script"

mcp_script = ROOT / args[-1]
assert mcp_script.exists(), f"MCP server script {mcp_script} referenced by mcp.json does not exist"

print("OK: mcp.json is structurally valid")
