"""tests/test_mcp/test_jsonrpc.py —— 三形态编解码往返（M05 §5）。"""
from __future__ import annotations

import pytest

from agent_godot.mcp.client.jsonrpc import (McpRemoteError, RPCNotification,
                                            RPCRequest, RPCResponse, decode,
                                            encode)


def test_request_roundtrip():
    line = encode(RPCRequest("tools/call", {"name": "echo"}, 7))
    assert "\n" not in line                       # ★ 单行（stdio 分帧铁律）
    assert "  " not in line                       # 无 indent
    msg = decode(line)
    assert isinstance(msg, RPCRequest)
    assert msg.method == "tools/call" and msg.id == 7


def test_notification_roundtrip():
    line = encode(RPCNotification("notifications/initialized"))
    assert "id" not in line                       # 通知无 id
    msg = decode(line)
    assert isinstance(msg, RPCNotification)


def test_response_decode_success_and_error():
    ok = decode('{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')
    assert isinstance(ok, RPCResponse) and ok.result == {"tools": []}
    err = decode('{"jsonrpc":"2.0","id":2,"error":{"code":-32601,"message":"no"}}')
    assert err.error["code"] == -32601
    e = McpRemoteError(err.error)
    assert "[-32601]" in str(e)


def test_params_omitted_when_none():
    line = encode(RPCNotification("ping"))
    assert "params" not in line
