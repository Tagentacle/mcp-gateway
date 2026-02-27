# MCP Gateway

> **The ROS of AI Agents** — Transport-level stdio→HTTP relay and MCP directory service.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

`mcp-gateway` adapts legacy stdio-only MCP servers to Streamable HTTP and provides unified MCP server discovery via the `/mcp/directory` Topic.

## Architecture

```
HTTP Client                       stdio subprocess
    │                                   │
    │  POST /my-tools  ───────────►     │
    │  (JSON-RPC msg)     relay         │  stdin (JSON-RPC)
    │                                   │
    │  ◄───────────────  relay          │
    │  SSE / JSON resp                  │  stdout (JSON-RPC)
    │                                   │
```

**Key design**: The Gateway is a pure transport bridge — it relays raw `SessionMessage` objects between HTTP and stdio without parsing MCP tools, resources, or prompts. This preserves full MCP protocol fidelity including sampling and notifications.

## Features

- **StdioRelay**: Duck-types as MCP Server for `StreamableHTTPSessionManager`. Spawns one stdio subprocess per HTTP session.
- **Remote Directory**: Publishes URLs of known remote Streamable HTTP servers to `/mcp/directory`.
- **Bus Services**: `/mcp/gateway/list_servers`, `/mcp/gateway/server_info`.
- **Tool Probing**: Brief `initialize()` handshake to discover tool summaries for directory entries.
- **TOML Configuration**: Declare stdio and remote servers in a config file.
- **Health Endpoint**: `GET /health` for liveness probes.

## Configuration

Create a TOML config file (path via CLI arg or `MCP_GATEWAY_CONFIG` env var):

```toml
[[stdio_servers]]
name = "my-legacy-tool"
command = "python"
args = ["-m", "my_tool.server"]
endpoint = "/my-legacy-tool"

[[remote_servers]]
server_id = "weather-server"
url = "http://10.0.0.5:8200/mcp"
description = "Weather tool service"
```

### Environment Overrides

| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_GATEWAY_CONFIG` | Path to TOML config file | — |
| `MCP_GATEWAY_HOST` | Bind address | `0.0.0.0` |
| `MCP_GATEWAY_PORT` | Bind port | `8100` |

## Run

```bash
# Via Tagentacle CLI
tagentacle run --pkg .

# Directly
python gateway.py /path/to/config.toml
```

## /mcp/directory Topic

The Gateway publishes `MCPServerDescription` messages (see `msg/MCPServerDescription.json`) to the `/mcp/directory` Topic. Agent Nodes subscribe to auto-discover available MCP servers.

## Tagentacle Pkg

This is a Tagentacle **executable pkg** (`type = "executable"` in `tagentacle.toml`).

- **Entry point**: `gateway:main`
- **Dependencies**: `tagentacle-py-core`, `mcp>=1.8`, `anyio`, `uvicorn`, `starlette`

## License

MIT
