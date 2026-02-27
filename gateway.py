"""
MCPGatewayNode — transport-level stdio→HTTP relay + directory service.

Responsibilities:
  1. stdio relay: Spawn stdio MCP servers and expose them via Streamable HTTP,
     forwarding raw SessionMessage objects WITHOUT understanding MCP semantics.
  2. Remote directory: Publish known remote server URLs to /mcp/directory.
  3. Bus Services: /mcp/gateway/list_servers, /mcp/gateway/server_info.

Architecture:
  - StdioRelay duck-types as an MCP Server app for StreamableHTTPSessionManager.
  - Each HTTP session spawns a fresh stdio subprocess; messages are bidirectionally
    relayed at the transport level (SessionMessage ↔ SessionMessage).
  - The Gateway never parses tools/resources/prompts — it's a pure transport bridge.
  - Remote servers are not proxied; their URLs are published to /mcp/directory.

    HTTP Client                       stdio subprocess
        │                                   │
        │  POST /my-tools  ───────────►     │
        │  (JSON-RPC msg)     relay         │  stdin (JSON-RPC)
        │                                   │
        │  ◄───────────────  relay          │
        │  SSE / JSON resp                  │  stdout (JSON-RPC)
        │                                   │

Usage (via bringup system_launch.toml):
    [nodes.mcp_gateway]
    package = "mcp-gateway"
    config.host = "0.0.0.0"
    config.port = 8100
    config.stdio_servers = [
        { name = "my-tools", command = "python", args = ["-m", "my_tools_server"] },
    ]
    config.remote_servers = [
        { name = "cloud-llm", url = "http://remote:9000/mcp" },
    ]
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from tagentacle_py_core import LifecycleNode

logger = logging.getLogger("tagentacle.mcp.gateway")

MCP_DIRECTORY_TOPIC = "/mcp/directory"


# ---------------------------------------------------------------------------
# Data classes for configuration
# ---------------------------------------------------------------------------

@dataclass
class StdioServerConfig:
    """Configuration for a stdio MCP server to be relayed."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    description: str = ""


@dataclass
class RemoteServerConfig:
    """Configuration for a remote MCP server (URL only, no proxy)."""
    name: str
    url: str
    description: str = ""


# ---------------------------------------------------------------------------
# StdioRelay — transport-level bridge, duck-types as MCP Server app
# ---------------------------------------------------------------------------

class StdioRelay:
    """Transport-level relay that bridges HTTP ↔ stdio by forwarding
    raw ``SessionMessage`` objects, without parsing MCP semantics.

    Duck-types as an MCP Server for ``StreamableHTTPSessionManager``.
    Each call to ``run()`` spawns a **fresh** stdio subprocess — one
    subprocess per HTTP session, with proper cleanup on disconnect.

    This means the Gateway is protocol-agnostic: any current or future
    MCP request type (tools, resources, prompts, sampling, …) passes
    through transparently.
    """

    def __init__(self, params: StdioServerParameters):
        self.params = params

    def create_initialization_options(self):
        """Required by StreamableHTTPSessionManager. Ignored by run()."""
        return None

    async def run(
        self,
        read_stream,
        write_stream,
        _init_options,
        *,
        raise_exceptions: bool = False,
        stateless: bool = False,
    ):
        """Spawn a stdio subprocess and bidirectionally relay messages.

        - ``read_stream``:  incoming from HTTP client  → forward to subprocess stdin
        - ``write_stream``: outgoing from subprocess stdout → forward to HTTP client
        """
        async with stdio_client(self.params) as (stdio_read, stdio_write):
            async with anyio.create_task_group() as tg:

                async def http_to_stdio():
                    """Forward HTTP → subprocess stdin."""
                    async for msg in read_stream:
                        if isinstance(msg, Exception):
                            continue
                        await stdio_write.send(msg)
                    # HTTP side closed (client disconnected) → stop relay
                    tg.cancel_scope.cancel()

                async def stdio_to_http():
                    """Forward subprocess stdout → HTTP."""
                    async for msg in stdio_read:
                        if isinstance(msg, Exception):
                            continue
                        await write_stream.send(msg)
                    # stdio side closed (process exited) → stop relay
                    tg.cancel_scope.cancel()

                tg.start_soon(http_to_stdio)
                tg.start_soon(stdio_to_http)


# ---------------------------------------------------------------------------
# MCPGatewayNode — LifecycleNode that orchestrates relays and directory
# ---------------------------------------------------------------------------

class MCPGatewayNode(LifecycleNode):
    """Gateway Node: stdio→HTTP transport relay + remote server directory.

    Lifecycle:
      - on_configure: parse config (stdio_servers, remote_servers).
      - on_activate:  optionally probe tool lists, create relays,
        start uvicorn, publish /mcp/directory entries.
      - on_deactivate: publish unavailable, stop uvicorn.
    """

    def __init__(self, node_id: str = "mcp_gateway"):
        super().__init__(node_id)
        self._host = "0.0.0.0"
        self._port = 8100
        self._stdio_configs: Dict[str, StdioServerConfig] = {}
        self._remote_servers: Dict[str, RemoteServerConfig] = {}
        self._relays: Dict[str, StdioRelay] = {}
        self._session_managers: Dict[str, StreamableHTTPSessionManager] = {}
        self._tools_cache: Dict[str, list[str]] = {}  # for /mcp/directory
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._serve_task: Optional[asyncio.Task] = None

    # ---- lifecycle hooks ----

    def on_configure(self, config: Dict[str, Any]):
        """Parse gateway configuration."""
        self._host = config.get("host", self._host)
        self._port = int(config.get("port", self._port))

        for entry in config.get("stdio_servers", []):
            cfg = StdioServerConfig(
                name=entry["name"],
                command=entry["command"],
                args=entry.get("args", []),
                env=entry.get("env"),
                cwd=entry.get("cwd"),
                description=entry.get("description", ""),
            )
            self._stdio_configs[cfg.name] = cfg

        for entry in config.get("remote_servers", []):
            cfg = RemoteServerConfig(
                name=entry["name"],
                url=entry["url"],
                description=entry.get("description", ""),
            )
            self._remote_servers[cfg.name] = cfg

        # Register bus services
        self.services["/mcp/gateway/list_servers"] = self._svc_list_servers
        self.services["/mcp/gateway/server_info"] = self._svc_server_info

        logger.info(
            "Gateway configured — %d stdio server(s), %d remote server(s)",
            len(self._stdio_configs),
            len(self._remote_servers),
        )

    async def on_activate(self):
        """Start relays, HTTP server, and publish /mcp/directory."""
        # 1. Probe each stdio server for tool names (best effort)
        for name, cfg in self._stdio_configs.items():
            self._tools_cache[name] = await self._probe_tools(cfg)

        # 2. Create relay + session manager per stdio server
        for name, cfg in self._stdio_configs.items():
            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args,
                env=cfg.env,
                cwd=cfg.cwd,
            )
            relay = StdioRelay(params)
            self._relays[name] = relay
            self._session_managers[name] = StreamableHTTPSessionManager(
                app=relay,
                json_response=False,
                stateless=False,
            )

        # 3. Build Starlette app and start uvicorn
        app = self._build_starlette_app()
        uvi_config = uvicorn.Config(
            app, host=self._host, port=self._port, log_level="info",
        )
        self._uvicorn_server = uvicorn.Server(uvi_config)
        self._serve_task = asyncio.create_task(self._uvicorn_server.serve())
        await asyncio.sleep(0.3)

        # 4. Publish to /mcp/directory
        base_url = f"http://{self._host}:{self._port}"
        for name, cfg in self._stdio_configs.items():
            await self._publish_directory(
                server_id=name,
                url=f"{base_url}/{name}",
                source="gateway",
                status="available",
                tools_summary=self._tools_cache.get(name, []),
                description=cfg.description,
            )
        for name, cfg in self._remote_servers.items():
            await self._publish_directory(
                server_id=name,
                url=cfg.url,
                source="gateway",
                status="available",
                tools_summary=[],
                description=cfg.description,
            )

        logger.info("MCP Gateway active at %s", base_url)

    async def on_deactivate(self):
        """Stop HTTP server, publish unavailable status."""
        for name in list(self._stdio_configs) + list(self._remote_servers):
            try:
                await self._publish_directory(
                    name, "", "gateway", "unavailable", [], "",
                )
            except Exception:
                pass

        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        if self._serve_task:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
            self._serve_task = None
        self._uvicorn_server = None
        self._session_managers.clear()
        self._relays.clear()
        logger.info("MCP Gateway deactivated")

    async def on_shutdown(self):
        """Ensure HTTP server is stopped on shutdown."""
        if self._uvicorn_server and self._serve_task and not self._serve_task.done():
            self._uvicorn_server.should_exit = True
            try:
                await asyncio.wait_for(self._serve_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()

    # ---- tool probe ----

    async def _probe_tools(self, cfg: StdioServerConfig) -> list[str]:
        """Spawn a temporary subprocess to discover tool names, then kill it.

        This is the ONLY place the Gateway touches MCP semantics — a brief
        handshake to populate /mcp/directory with useful tool summaries.
        """
        try:
            params = StdioServerParameters(
                command=cfg.command, args=cfg.args,
                env=cfg.env, cwd=cfg.cwd,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    names = [t.name for t in result.tools]
                    logger.info(
                        "Probed '%s': %d tools — %s",
                        cfg.name, len(names), names,
                    )
                    return names
        except Exception as exc:
            logger.warning("Failed to probe tools for '%s': %s", cfg.name, exc)
            return []

    # ---- Starlette app ----

    def _build_starlette_app(self) -> Starlette:
        """Build a Starlette ASGI app that mounts each relay at ``/{name}``
        and manages session-manager lifespans.
        """
        managers = dict(self._session_managers)

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette):
            async with contextlib.AsyncExitStack() as stack:
                for mgr in managers.values():
                    await stack.enter_async_context(mgr.run())
                yield

        routes: list = []
        for name, mgr in managers.items():
            routes.append(Mount(f"/{name}", app=mgr.handle_request))

        async def health(request: Request) -> Response:
            info = {
                "node": self.node_id,
                "stdio_servers": {
                    n: self._tools_cache.get(n, [])
                    for n in self._stdio_configs
                },
                "remote_servers": {
                    n: c.url for n, c in self._remote_servers.items()
                },
            }
            return Response(
                content=json.dumps(info, indent=2),
                media_type="application/json",
            )

        routes.append(Route("/", health))
        return Starlette(routes=routes, lifespan=lifespan)

    # ---- /mcp/directory publishing ----

    async def _publish_directory(
        self, server_id, url, source, status, tools_summary, description,
    ):
        """Publish an MCPServerDescription to /mcp/directory Topic."""
        await self.publish(MCP_DIRECTORY_TOPIC, {
            "server_id": server_id,
            "url": url,
            "transport": "streamable-http",
            "status": status,
            "source": source,
            "tools_summary": tools_summary,
            "description": description,
            "publisher_node_id": self.node_id,
        })

    # ---- bus service handlers ----

    async def _svc_list_servers(self, _payload):
        """Handle /mcp/gateway/list_servers service call."""
        base = f"http://{self._host}:{self._port}"
        servers = []
        for name, cfg in self._stdio_configs.items():
            servers.append({
                "name": name,
                "type": "stdio",
                "url": f"{base}/{name}",
                "tools": self._tools_cache.get(name, []),
                "description": cfg.description,
            })
        for name, cfg in self._remote_servers.items():
            servers.append({
                "name": name,
                "type": "remote",
                "url": cfg.url,
                "description": cfg.description,
            })
        return json.dumps({"servers": servers})

    async def _svc_server_info(self, payload):
        """Handle /mcp/gateway/server_info service call."""
        data = json.loads(payload) if isinstance(payload, str) else payload
        name = data.get("server_id", "")
        base = f"http://{self._host}:{self._port}"

        if name in self._stdio_configs:
            cfg = self._stdio_configs[name]
            return json.dumps({
                "name": name,
                "type": "stdio",
                "url": f"{base}/{name}",
                "tools": self._tools_cache.get(name, []),
                "command": cfg.command,
                "args": cfg.args,
                "description": cfg.description,
            })
        elif name in self._remote_servers:
            cfg = self._remote_servers[name]
            return json.dumps({
                "name": name,
                "type": "remote",
                "url": cfg.url,
                "description": cfg.description,
            })
        return json.dumps({"error": f"Server '{name}' not found"})


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def _load_toml(path: str) -> dict:
    """Load a TOML file into a dict."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redefine]
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_gateway_config() -> Dict[str, Any]:
    """Build gateway config from file + environment variables.

    Resolution order (later overrides earlier):
      1. Config file  — path from CLI arg or ``MCP_GATEWAY_CONFIG`` env var.
         Expected TOML structure::

             host = "0.0.0.0"
             port = 8100

             [[stdio_servers]]
             name = "my-tools"
             command = "python"
             args = ["-m", "my_tools_server"]
             description = "My tool server"

             [[remote_servers]]
             name = "cloud-llm"
             url = "http://remote:9000/mcp"

      2. Environment variables (override file values):
         - ``MCP_GATEWAY_HOST``  → config["host"]
         - ``MCP_GATEWAY_PORT``  → config["port"]
    """
    config: Dict[str, Any] = {}

    # 1. Config file
    config_path = None
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = os.environ.get("MCP_GATEWAY_CONFIG")

    if config_path and os.path.isfile(config_path):
        config = _load_toml(config_path)
        logger.info("Loaded gateway config from %s", config_path)
    elif config_path:
        logger.warning("Config file not found: %s (using defaults)", config_path)

    # 2. Environment variable overrides
    env_host = os.environ.get("MCP_GATEWAY_HOST")
    if env_host:
        config["host"] = env_host

    env_port = os.environ.get("MCP_GATEWAY_PORT")
    if env_port:
        config["port"] = int(env_port)

    return config


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    """Entry point for the MCP Gateway Node."""
    config = load_gateway_config()

    node = MCPGatewayNode()
    await node.connect()

    spin_task = asyncio.create_task(node.spin())

    await node.configure(config)
    await node.activate()

    try:
        await spin_task
    except asyncio.CancelledError:
        pass
    finally:
        if node.state.value == "active":
            await node.deactivate()
        await node.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
