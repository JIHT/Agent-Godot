"""mcp/client/bridge.py —— MCP 工具 → FC 注册表（M05 §1.5 / §4 步骤 4）

外籍员工入职翻译：外部服务商的工具进店前，HR（bridge）给它办工牌——
- 姓名加部门前缀 mcp__{server}__{name}（防跨服务器/本地工具重名）
- 简历格式转换（inputSchema 经 clean_schema 清洗成 FC parameters）
- 风险标注：MCP 不带 readonly/risk 元数据 → 名称启发式
  （write/delete/create/edit/run/execute → 写类 medium）+ 服务器级默认

M04 的 ToolRegistry 完全无感（它不知道也不需要知道工具来自 MCP）——
这正是 M00"一切皆插件"最完整的一次兑现：mcp.yaml 加一段配置，
Agent 工具箱就多一个服务器的全部工具，核心代码零改动。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import yaml

from agent_godot.tools import (BaseTool, ErrorKind, ToolError, ToolMeta,
                               ToolRegistry, ToolResponse)
from agent_godot.tools.schema import clean_schema

from .session import McpSession
from .transport import HttpTransport, StdioTransport, Transport

logger = logging.getLogger(__name__)

# 名称启发式：含这些动词的 MCP 工具视为"有副作用"（写类，Dispatcher 按序执行）
_WRITE_HINTS = ("write", "delete", "create", "edit", "run", "execute",
                "download", "post", "put", "remove")


def _looks_readonly(name: str) -> bool:
    return not any(h in name.lower() for h in _WRITE_HINTS)


class McpToolBridge(BaseTool):
    """把一个 MCP 工具包装成 BaseTool（办工牌的外籍员工）。

    覆写 execute()：跳过本地 pydantic 校验（inputSchema 是服务器运行时
    给的动态 dict，无法静态建模型）——参数校验交给服务器端，各端各司其职。
    """

    def __init__(self, server_name: str, tool_info: dict, session: McpSession):
        self.meta = ToolMeta(
            name=f"mcp__{server_name}__{tool_info['name']}",
            description=tool_info.get("description", ""),
            readonly=_looks_readonly(tool_info["name"]),
            risk="medium")
        self._tool_name = tool_info["name"]
        self._session = session
        self._input_schema = tool_info.get("inputSchema",
                                           {"type": "object", "properties": {}})

    async def execute(self, arguments: str) -> ToolResponse:
        """覆写：JSON 解析在本地，结构校验交给服务器。"""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.VALIDATION, self.meta.name,
                f"参数不是合法 JSON: {arguments[:80]!r}",
                hint="按工具 inputSchema 修正后重试"))
        return await self._session.call_tool(self._tool_name, args)

    async def run(self, **params) -> ToolResponse:
        return await self._session.call_tool(self._tool_name, params)

    def to_spec(self):
        """覆写：直接用服务器的 inputSchema（经清洗），不走 pydantic。"""
        from agent_godot.core import ToolSpec
        return ToolSpec(self.meta.name, self.meta.description,
                        clean_schema(self._input_schema))


class McpManager:
    """读 config/mcp.yaml，管理全部服务器会话的启停与桥接。

    容错原则：单个服务器启动失败只记日志不炸整体（Agent 还有本地工具）；
    服务器中途崩溃由 session 的死亡清算兜底（调用秒级失败并提示）。
    """

    def __init__(self, registry: ToolRegistry,
                 config_path: str | Path | None = None):
        self.registry = registry
        self._sessions: dict[str, McpSession] = {}
        self._disabled: set[str] = set()
        self._config_path = Path(config_path) if config_path else self._find_config()

    @staticmethod
    def _find_config() -> Path:
        for cand in ("config/mcp.yaml", "../config/mcp.yaml",
                     "../../config/mcp.yaml"):
            if Path(cand).exists():
                return Path(cand)
        return Path("config/mcp.yaml")           # 不存在则 start_all 空转

    async def start_all(self) -> None:
        """逐服务器：造传输 → 握手 → 桥接工具进 registry。失败不炸整体。"""
        if not self._config_path.exists():
            logger.info("未找到 %s，跳过 MCP 接入", self._config_path)
            return
        with self._config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        for name, cfg in (config.get("servers") or {}).items():
            if not cfg.get("enabled", False):
                self._disabled.add(name)
                continue
            try:
                transport = self._make_transport(cfg)
                await transport.start()          # ★ 起进程/建连接
                session = McpSession(transport, timeout=float(cfg.get("timeout", 60)))
                await session.initialize()
                await self._bridge_server(name, session)
                self._sessions[name] = session
                logger.info("MCP 服务器 %s 就绪（工具已桥接）", name)
            except Exception as e:               # noqa: BLE001 —— 不拖死 Agent
                logger.warning("MCP 服务器 %s 启动失败: %s", name, e)

    async def _bridge_server(self, name: str, session: McpSession) -> None:
        """把该服务器的全部工具包装注册（命名空间 mcp__{server}__{name}）。"""
        for tool_info in await session.list_tools():
            self.registry.register(McpToolBridge(name, tool_info, session))

    @staticmethod
    def _make_transport(cfg: dict) -> Transport:
        kind = cfg.get("transport", "stdio")
        if kind == "stdio":
            return StdioTransport(cfg["command"], cfg.get("args"),
                                  env=cfg.get("env"), cwd=cfg.get("cwd"))
        if kind == "http":
            return HttpTransport(cfg["url"], headers=cfg.get("headers"))
        raise ValueError(f"未知 transport: {kind}（支持 stdio/http）")

    def server_status(self) -> dict[str, Literal["running", "dead", "disabled"]]:
        status = {name: "disabled" for name in self._disabled}
        status.update({name: ("dead" if s._dead else "running")
                       for name, s in self._sessions.items()})
        return status

    async def stop_all(self) -> None:
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception:                    # noqa: BLE001 —— 关闭尽力而为
                pass
        self._sessions.clear()
