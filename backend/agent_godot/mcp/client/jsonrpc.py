"""mcp/client/jsonrpc.py —— JSON-RPC 2.0 编解码（M05 §1.2 / §4 步骤 1）

餐厅点餐小票系统：请求=点菜单（有编号 id）、响应=叫号出餐（result 或
error 二选一）、通知=喊一嗓子（无 id，不等回应）。id 是异步关联键——
并发发出 1/2/3，响应乱序回来全靠 id 配对（session 的 pending 字典消费它）。

铁律：消息必须**单行 JSON**（stdio 按 \\n 分帧）——encode 绝不能加 indent。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class RPCRequest:
    """请求：期待响应（有 id）。"""
    method: str
    params: dict | None
    id: str | int


@dataclass
class RPCNotification:
    """通知：单向告知（无 id，无响应——发出后绝不能"等"）。"""
    method: str
    params: dict | None = None


@dataclass
class RPCResponse:
    """响应：result 与 error 二选一。"""
    id: str | int
    result: Any = None
    error: dict | None = None      # {"code": -32601, "message": "..."}


class McpRemoteError(Exception):
    """服务器在协议层返回的 error（JSON-RPC error 对象）。"""

    def __init__(self, error: dict):
        self.code = error.get("code")
        self.message = error.get("message", "")
        super().__init__(f"[{self.code}] {self.message}")


def encode(msg: RPCRequest | RPCNotification) -> str:
    """序列化为单行 JSON。★ 禁 indent——多行 JSON 直接破 stdio 分帧协议。"""
    d: dict = {"jsonrpc": "2.0", "method": msg.method}
    if msg.params is not None:
        d["params"] = msg.params
    if isinstance(msg, RPCRequest):
        d["id"] = msg.id
    return json.dumps(d, ensure_ascii=False)


def decode(raw: str) -> RPCRequest | RPCNotification | RPCResponse:
    """反序列化：有 method 键 = 请求/通知（有无 id 区分），否则是响应。"""
    d = json.loads(raw)
    if "method" in d:
        if "id" in d:
            return RPCRequest(d["method"], d.get("params"), d["id"])  # 服务器反向请求
        return RPCNotification(d["method"], d.get("params"))
    return RPCResponse(d.get("id"), d.get("result"), d.get("error"))
