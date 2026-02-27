# Changelog — mcp-gateway

All notable changes to the **mcp-gateway** package will be documented in this file.
For related changes see [`python-sdk-mcp`](https://github.com/Tagentacle/python-sdk-mcp) and [`tagentacle`](https://github.com/Tagentacle/tagentacle) changelogs.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-03-15

### Added
- **MCPGatewayNode** (`LifecycleNode` subclass):
  - Transport-level stdio→HTTP relay via `StdioRelay`.
  - Remote MCP server directory publishing to `/mcp/directory` Topic.
  - Bus services: `/mcp/gateway/list_servers`, `/mcp/gateway/server_info`.
  - `/health` endpoint for liveness probes.
- **StdioRelay**:
  - Duck-types as MCP Server for `StreamableHTTPSessionManager`.
  - Spawns stdio subprocess per HTTP session; bidirectionally relays raw `SessionMessage` objects.
  - Protocol-agnostic — never parses MCP tools/resources/prompts.
- **MCPServerDescription** (schema now in separate [`mcp-interfaces`](https://github.com/Tagentacle/mcp-interfaces) package):
  - Published on `/mcp/directory` Topic for unified MCP server discovery.
- **TOML Configuration**:
  - `[[stdio_servers]]` — declare stdio-based MCP servers with command, args, env, endpoint path.
  - `[[remote_servers]]` — declare remote Streamable HTTP servers with URL and metadata.
  - Config file via CLI arg or `MCP_GATEWAY_CONFIG` env var.
  - `MCP_GATEWAY_HOST` / `MCP_GATEWAY_PORT` env overrides.
- **Tool Probing**: Brief `initialize()` handshake to discover tool summaries for directory entries.
- **Tagentacle pkg manifest**: `tagentacle.toml` with `type = "executable"`, entry point `gateway:main`.
