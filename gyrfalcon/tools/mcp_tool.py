"""MCP client tool — connects to external MCP servers and registers their tools dynamically."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.tools import registry

logger = get_logger("tools.mcp")

_mcp_connections: dict[str, "MCPConnection"] = {}
_lock = threading.Lock()


# ── McpStore ──────────────────────────────────────────────────────────────────

class McpStore:
    """Persistent store for MCP server configs in ~/.gyrfalcon/mcp/mcp.json."""

    def _path(self) -> Path:
        from gyrfalcon.gyrfalcon_constants import get_mcp_file
        return get_mcp_file()

    def load(self) -> dict[str, dict]:
        """Return {name: config_dict} for all configured servers."""
        p = self._path()
        if not p.exists():
            # Migrate from config.yaml if mcp.json doesn't exist yet
            return self._migrate_from_config()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, servers: dict[str, dict]) -> None:
        """Overwrite mcp.json with the given dict."""
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(servers, indent=2), encoding="utf-8")

    def get(self, name: str) -> Optional[dict]:
        return self.load().get(name)

    def put(self, name: str, config: dict) -> None:
        servers = self.load()
        servers[name] = config
        self.save(servers)

    def remove(self, name: str) -> bool:
        servers = self.load()
        if name not in servers:
            return False
        del servers[name]
        self.save(servers)
        return True

    def _migrate_from_config(self) -> dict[str, dict]:
        """One-time migration: copy mcp.servers from config.yaml to mcp.json,
        then remove the mcp block from config.yaml so it stays clean."""
        try:
            from gyrfalcon.config import load_config, save_config
            config = load_config()
            mcp_block = config.pop("mcp", {}) or {}
            servers = mcp_block.get("servers", {}) or {}
            if servers:
                logger.info(f"Migrating {len(servers)} MCP server(s) from config.yaml -> mcp.json")
                self.save(servers)
                # Remove mcp block from config.yaml now that it's migrated
                save_config(config)
                logger.info("Removed mcp block from config.yaml")
            return servers
        except Exception as e:
            logger.warning(f"MCP migration failed: {e}")
            return {}


mcp_store = McpStore()


class MCPConnection:
    """Connection to an MCP server (stdio transport)."""

    def __init__(self, server_name: str, command: list[str], env: dict[str, str] | None = None):
        self.server_name = server_name
        self.command = command
        self.env = env
        self._process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._tools: dict[str, dict] = {}

    def connect(self) -> bool:
        """Start MCP server process and initialize."""
        logger.debug("Beginning of connect")
        try:
            import os
            proc_env = os.environ.copy()
            if self.env:
                proc_env.update(self.env)

            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=proc_env,
            )

            # Send initialize request
            result = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gyrfalcon", "version": "0.1.0"},
            })

            if result:
                # Send initialized notification
                self._send_notification("notifications/initialized", {})
                # Discover tools
                self._discover_tools()
                return True

            return False
        except Exception as e:
            logger.error(f"Failed to connect to MCP server {self.server_name}: {e}")
            return False

    def disconnect(self) -> None:
        """Shutdown MCP server."""
        logger.debug("Beginning of disconnect")
        if self._process:
            try:
                self._process.stdin.close()
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                if self._process:
                    self._process.kill()
            self._process = None

    def _send_request(self, method: str, params: dict) -> Optional[dict]:
        """Send JSON-RPC request and wait for response."""
        logger.debug("Beginning of _send_request")
        if not self._process or not self._process.stdin or not self._process.stdout:
            return None

        with self._lock:
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }

            try:
                line = json.dumps(request) + "\n"
                self._process.stdin.write(line.encode())
                self._process.stdin.flush()

                # Read response
                response_line = self._process.stdout.readline()
                if response_line:
                    response = json.loads(response_line)
                    if "error" in response:
                        logger.error(f"MCP error: {response['error']}")
                        return None
                    return response.get("result")
                return None
            except Exception as e:
                logger.error(f"MCP request failed: {e}")
                return None

    def _send_notification(self, method: str, params: dict) -> None:
        """Send JSON-RPC notification (no response expected)."""
        logger.debug("Beginning of _send_notification")
        if not self._process or not self._process.stdin:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        try:
            line = json.dumps(notification) + "\n"
            self._process.stdin.write(line.encode())
            self._process.stdin.flush()
        except Exception:
            pass

    def _discover_tools(self) -> None:
        """Discover tools from MCP server."""
        logger.debug("Beginning of _discover_tools")
        result = self._send_request("tools/list", {})
        if not result:
            return

        tools = result.get("tools", [])
        for tool in tools:
            tool_name = f"mcp_{self.server_name}_{tool['name']}"
            schema = {
                "name": tool_name,
                "description": tool.get("description", f"MCP tool: {tool['name']}"),
                "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
            }
            self._tools[tool_name] = tool

            # Register in global registry
            registry.register(
                name=tool_name,
                toolset=f"mcp-{self.server_name}",
                schema=schema,
                handler=self._make_handler(tool["name"]),
            )

        logger.info(f"MCP server {self.server_name}: registered {len(tools)} tools")

    def _make_handler(self, original_name: str):
        """Create a handler function for an MCP tool."""
        logger.debug("Beginning of _make_handler")
        def handler(args: dict, **kwargs) -> str:
            logger.debug("Beginning of handler")
            return self.call_tool(original_name, args)
        return handler

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on the MCP server."""
        logger.debug("Beginning of call_tool")
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        if result is None:
            return json.dumps({"error": "MCP tool call failed"})

        # Extract text content from result
        content = result.get("content", [])
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)

        if text_parts:
            return "\n".join(text_parts)
        return json.dumps(result)


def initialize_mcp_servers() -> None:
    """Initialize all configured MCP servers from ~/.gyrfalcon/mcp/mcp.json."""
    logger.debug("Beginning of initialize_mcp_servers")
    servers_config = mcp_store.load()
    if not servers_config:
        return

    for server_name, config in servers_config.items():
        if not isinstance(config, dict):
            continue
        if not config.get("enabled", True):
            continue

        server_type = config.get("type", "stdio")
        if server_type not in ("stdio", None, ""):
            # odata and openapi servers are not stdio — skip auto-connect
            logger.debug(f"Skipping non-stdio MCP server: {server_name} (type={server_type})")
            continue

        command = config.get("command", [])
        if isinstance(command, str):
            command = command.split()

        if not command:
            continue

        env = config.get("env", {})
        args = config.get("args", [])
        full_command = command + args

        conn = MCPConnection(server_name, full_command, env)
        if conn.connect():
            with _lock:
                _mcp_connections[server_name] = conn
            logger.info(f"Connected to MCP server: {server_name}")
        else:
            logger.warning(f"Failed to connect to MCP server: {server_name}")


def shutdown_mcp_servers() -> None:
    """Disconnect all MCP servers."""
    logger.debug("Beginning of shutdown_mcp_servers")
    with _lock:
        for name, conn in _mcp_connections.items():
            conn.disconnect()
            logger.info(f"Disconnected MCP server: {name}")
        _mcp_connections.clear()


def get_mcp_connection(server_name: str) -> Optional[MCPConnection]:
    """Get an active MCP connection."""
    logger.debug("Beginning of get_mcp_connection")
    return _mcp_connections.get(server_name)


def list_mcp_servers() -> list[str]:
    """List connected MCP server names."""
    logger.debug("Beginning of list_mcp_servers")
    return list(_mcp_connections.keys())
