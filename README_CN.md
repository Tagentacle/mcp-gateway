# MCP Gateway（MCP 网关）

> **The ROS of AI Agents** — 传输层 stdio→HTTP 中继及 MCP 目录服务。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

`mcp-gateway` 将仅支持 stdio 的传统 MCP Server 适配为 Streamable HTTP，并通过 `/mcp/directory` Topic 提供统一的 MCP 服务器发现。

## 架构

```
HTTP Client                       stdio 子进程
    │                                   │
    │  POST /my-tools  ───────────►     │
    │  (JSON-RPC msg)     中继          │  stdin (JSON-RPC)
    │                                   │
    │  ◄───────────────  中继           │
    │  SSE / JSON 响应                  │  stdout (JSON-RPC)
    │                                   │
```

**核心设计**：Gateway 是纯传输层桥接 — 在 HTTP 和 stdio 之间中继原始 `SessionMessage` 对象，不解析 MCP 工具/资源/提示。完整保留 MCP 协议能力，包括 sampling 和通知。

## 功能

- **StdioRelay**：鸭子类型模拟 MCP Server，供 `StreamableHTTPSessionManager` 使用。每个 HTTP 会话启动一个 stdio 子进程。
- **远程目录**：将已知远程 Streamable HTTP 服务器 URL 发布到 `/mcp/directory`。
- **总线服务**：`/mcp/gateway/list_servers`、`/mcp/gateway/server_info`。
- **工具探测**：通过简短 `initialize()` 握手发现工具摘要，写入目录条目。
- **TOML 配置**：在配置文件中声明 stdio 和远程服务器。
- **健康检查端点**：`GET /health`。

## 配置

创建 TOML 配置文件（路径通过 CLI 参数或 `MCP_GATEWAY_CONFIG` 环境变量指定）：

```toml
[[stdio_servers]]
name = "my-legacy-tool"
command = "python"
args = ["-m", "my_tool.server"]
endpoint = "/my-legacy-tool"

[[remote_servers]]
server_id = "weather-server"
url = "http://10.0.0.5:8200/mcp"
description = "天气工具服务"
```

### 环境变量覆盖

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MCP_GATEWAY_CONFIG` | TOML 配置文件路径 | — |
| `MCP_GATEWAY_HOST` | 绑定地址 | `0.0.0.0` |
| `MCP_GATEWAY_PORT` | 绑定端口 | `8100` |

## 运行

```bash
# 通过 Tagentacle CLI
tagentacle run --pkg .

# 直接运行
python gateway.py /path/to/config.toml
```

## /mcp/directory Topic

Gateway 向 `/mcp/directory` Topic 发布 `MCPServerDescription` 消息（参见 `msg/MCPServerDescription.json`）。Agent 节点通过订阅该 Topic 自动发现可用 MCP 服务器。

## Tagentacle Pkg

这是一个 Tagentacle **executable pkg**（`tagentacle.toml` 中 `type = "executable"`）。

- **入口**：`gateway:main`
- **依赖**：`tagentacle-py-core`、`mcp>=1.8`、`anyio`、`uvicorn`、`starlette`

## 许可证

MIT
