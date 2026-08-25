"""mcp/client：MCP 客户端协议栈（M05）—— jsonrpc → transport → session → bridge。

四层就是协议栈，自底向上：消息格式 → 怎么送 → 何时送/怎么配对 → 翻译成 FC 工具。
"""
from .bridge import McpManager, McpToolBridge
from .jsonrpc import (McpRemoteError, RPCNotification, RPCRequest, RPCResponse,
                      decode, encode)
from .session import McpSession
from .transport import (HttpTransport, McpTransportError, StdioTransport,
                        Transport)

__all__ = [
    "McpManager", "McpToolBridge",
    "McpRemoteError", "RPCNotification", "RPCRequest", "RPCResponse",
    "decode", "encode",
    "McpSession",
    "HttpTransport", "McpTransportError", "StdioTransport", "Transport",
]
