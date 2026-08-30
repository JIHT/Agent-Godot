"""agent/a2a.py —— A2A 客户端：把外部 Agent 服务当"远程工人"派活（M15 §1.5）

Agent2Agent（Google 2025，Linux 基金会）：独立 Agent 服务间的互操作协议。
与 MCP 的分界（§2 问答 6）在交互对象的**自主性**：
    MCP = 工具级互操作（"借个锤子"：调用即返回，无自主决策）
    A2A = 代理级互操作（"把这面墙砌了"：接任务后自己规划，可能反问）
两者分层而非竞争——一个 A2A Agent 内部完全可能用 MCP 调工具。

三要素（§1.5 ①）：
  ① Agent Card：/.well-known/agent.json —— 自描述名片（会干什么/找谁/怎么签合同）
  ② Task 生命周期：submitted → working → input-required → completed / failed
  ③ Artifact：任务产物（文本/文件/结构化数据）

★ input-required 是 M09 确认门的跨公司版（§2 问答 7）：远程 Agent 说"缺料等
补充"，任务挂起。本协议层的挂起在这里**适配成本地确认门事件**——无论工人是
进程内还是远程，用户看到的都是同一个确认弹层（协议适配归 adapter，体验归产品）。

★ 不可信边界（§1.5 易错点）：任务书里不放密钥/内部绝对路径；产出物落地前过
验证。远程调用一律配超时（本地子代理秒级，远程分钟级——预算不是一个量级）。

最小可用子集（card 发现 + message/send + tasks/get 轮询）；SSE 订阅
（tasks/sendSubscribe）留给后续版本。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from agent_godot.core import Usage
from agent_godot.tools import ToolRegistry

from .subagents import Budget, SubagentSpec, SubtaskResult

logger = logging.getLogger(__name__)

# A2A 任务状态 → 本地 stop_reason（编排层据此决定重派/吞并/上抛）
TERMINAL = ("completed", "failed", "canceled", "rejected", "unknown")


class A2AError(RuntimeError):
    """A2A 协议错误（JSON-RPC error 对象 / HTTP 失败 / 卡片解析失败）。"""


# ---------- ① Agent Card：对方的名片 ----------

@dataclass
class AgentCard:
    """服务自描述（/.well-known/agent.json 的运行时镜像）。"""

    name: str
    description: str = ""
    endpoint: str = ""                  # JSON-RPC 端点（派活地址）
    auth: dict = field(default_factory=dict)   # schemes / credentials / token_env
    skills: list[str] = field(default_factory=list)
    version: str = ""
    base_url: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict, base_url: str = "") -> "AgentCard":
        """从 agent.json 解析（字段缺失按 A2A 草案兼容处理，不挑食）。"""
        if not isinstance(data, dict):
            raise A2AError(f"Agent Card 不是对象: {type(data).__name__}")
        url = str(data.get("url") or data.get("endpoint") or "").strip()
        auth = data.get("authentication") or data.get("auth") or {}
        if not isinstance(auth, dict):
            auth = {"credentials": str(auth)}
        skills = [str(s.get("name") or s.get("id") or "")
                  for s in (data.get("skills") or [])
                  if isinstance(s, dict)]
        return cls(
            name=str(data.get("name") or "").strip() or "unnamed-agent",
            description=str(data.get("description") or "").strip(),
            endpoint=url or base_url.rstrip("/"),
            auth=dict(auth),
            skills=[s for s in skills if s],
            version=str(data.get("version") or ""),
            base_url=base_url.rstrip("/"),
            raw=dict(data))

    @property
    def signature(self) -> str:
        """能力指纹（版本变了要重发现——§1.5 易错点③）。"""
        return f"{self.name}@{self.version or '0'}"

    def describe(self) -> str:
        skills = "、".join(self.skills[:5]) or "（未声明）"
        return (f"{self.name}（v{self.version or '?'}）\n"
                f"  端点: {self.endpoint}\n  能力: {skills}\n"
                f"  {self.description}")


# ---------- ② Task：工单状态机 ----------

@dataclass
class A2ATask:
    """远程任务的快照（Task 生命周期的一个状态点）。"""

    id: str = ""
    state: str = "submitted"            # submitted/working/input-required/…
    text: str = ""                      # 产物文本（artifacts 拼出来的正文）
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state in TERMINAL

    @property
    def ok(self) -> bool:
        return self.state == "completed"

    @classmethod
    def from_json(cls, data: dict) -> "A2ATask":
        """接受 JSON-RPC 信封（{"result": …}）或裸 task 对象。"""
        if not isinstance(data, dict):
            raise A2AError(f"任务响应不是对象: {type(data).__name__}")
        body = data.get("result", data)
        if not isinstance(body, dict):
            raise A2AError("任务响应的 result 不是对象")
        status = body.get("status") or {}
        state = str(status.get("state") or "submitted")
        text, artifacts = _collect_text(body)
        message = ""
        if isinstance(status.get("message"), dict):
            message, _ = _collect_text({"artifacts": [status["message"]]})
        return cls(id=str(body.get("id") or ""), state=state,
                   text=text or message, artifacts=artifacts,
                   error=_error_text(body.get("error") or status.get("error")),
                   raw=dict(body))


# ---------- ③ 客户端 ----------

class A2AClient:
    """A2A 最小客户端：发现名片 → 派活 → 轮询收工。

    transport：可注入的 HTTP 替身（测试用假传输，生产用 httpx.AsyncClient）。
    需要对象有 `async get/post(url, json=..., headers=...)`，返回值有
    `.json()`（可选 `.raise_for_status()`）。
    """

    def __init__(self, *, timeout: float = 30.0, poll_interval: float = 1.0,
                 max_polls: int = 60, transport=None, bus=None,
                 cache_ttl: float = 300.0):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self._transport = transport
        self._http = None
        self.bus = bus
        self.cache_ttl = cache_ttl                  # 名片缓存有效期（秒）
        self._cards: dict[str, AgentCard] = {}      # base_url → 缓存名片
        self._card_ts: dict[str, float] = {}        # base_url → 缓存时刻

    # ---------- 发现 ----------

    async def discover(self, base_url: str, force: bool = False) -> AgentCard:
        """拉 /.well-known/agent.json（带 TTL 缓存）。

        缓存与失效（§1.5 易错点③）：对方能力变了不会通知我，所以缓存必须
        有寿命——TTL 内直接复用（省一次往返），过期或 force=True 时重发现。
        重发现拿到新 card 后比对 version：变了就是能力漂移，记录一条事件
        （编排层据此决定是否重新拆解任务书）。
        """
        base = str(base_url).rstrip("/")
        cached = self._cards.get(base)
        fresh = time.monotonic() - self._card_ts.get(base, 0.0)
        if cached is not None and not force and fresh < self.cache_ttl:
            return cached
        data = await self._get(f"{base}/.well-known/agent.json")
        card = AgentCard.from_json(data, base_url=base)
        if cached is not None and card.signature != cached.signature:
            await self._emit("a2a_card_changed", name=card.name,
                             old=cached.version, new=card.version)
            logger.info("A2A 名片变更: %s %s → %s", card.name,
                        cached.version or "?", card.version or "?")
        self._cards[base] = card
        self._card_ts[base] = time.monotonic()
        await self._emit("a2a_discovered", name=card.name, version=card.version,
                         endpoint=card.endpoint)
        return card

    def cache_clear(self) -> None:
        self._cards.clear()
        self._card_ts.clear()

    # ---------- 派活与轮询 ----------

    async def send_task(self, card: AgentCard, text: str) -> str:
        """message/send → taskId（§1.5 ③）。"""
        payload = self._envelope("message/send", {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}]}})
        data = await self._post(card.endpoint, payload, card)
        task = A2ATask.from_json(data)
        if not task.id:
            raise A2AError(f"message/send 未返回 taskId: {str(data)[:200]}")
        await self._emit("a2a_task_sent", agent=card.name, task_id=task.id)
        return task.id

    async def poll(self, card: AgentCard, task_id: str) -> A2ATask:
        """tasks/get → 任务快照（SSE 订阅的降级形态：轮询）。"""
        payload = self._envelope("tasks/get", {"id": task_id})
        return A2ATask.from_json(
            await self._post(card.endpoint, payload, card))

    async def run_task(self, card: AgentCard, text: str) -> A2ATask:
        """send + poll 直到终态（远程排队可能分钟级，故轮询而非一次性等）。"""
        task_id = await self.send_task(card, text)
        task = A2ATask(id=task_id, state="submitted")
        for _ in range(self.max_polls):
            task = await self.poll(card, task_id)
            if task.state == "input-required":
                # 跨服务的"缺料等确认"→ 转成主控的确认门事件（§2 问答 7）
                await self._emit("a2a_input_required", agent=card.name,
                                 task_id=task_id, question=task.text)
                return task
            if task.done:
                return task
            await _sleep(self.poll_interval)
        task.state = "timeout"
        task.error = f"轮询 {self.max_polls} 次仍未终态"
        return task

    # ---------- 适配器：远程 Agent → 本地 SubagentSpec ----------

    def as_remote_worker(self, card: AgentCard, *,
                         name: str | None = None,
                         budget: Budget | None = None) -> SubagentSpec:
        """把 Agent Card 包装成子代理（run 时走 HTTP 而非本地 Loop）。

        这就是 §1.5 说的"又一个 Adapter 实战"：Orchestrator 分不出本地工人与
        外包工人——两边都吐 SubtaskResult，聚合管线零改动。
        """
        spec_name = name or f"a2a:{card.name}"

        async def _run(task: str, ctx: dict) -> SubtaskResult:
            try:
                remote = await self.run_task(card, task)
            except Exception as e:                  # noqa: BLE001 —— 远程不可信
                return SubtaskResult(spec_name=spec_name, ok=False,
                                     report=f"远程 A2A 调用失败: {e}",
                                     stop_reason="error")
            stop = {"completed": "natural", "input-required": "input_required",
                    "timeout": "timeout"}.get(remote.state, "error")
            report = remote.text or remote.error or "（远程任务无文本产物）"
            if remote.state == "input-required":
                report = f"【远程 Agent 需要补充信息】\n{report}"
            return SubtaskResult(
                spec_name=spec_name, ok=remote.ok, report=report,
                artifacts=list(remote.artifacts), usage=Usage(0, 0),
                stop_reason=stop)

        return SubagentSpec(
            name=spec_name,
            role_prompt=(card.description or f"远程专家 Agent：{card.name}"),
            tools=ToolRegistry(),               # 远程工人不用本地工具
            model="a2a",
            budget=budget or Budget(steps=1, tokens=0, usd=0.0,
                                    wall_time=max(self.timeout, 60.0)),
            description=f"A2A 远程服务（{card.endpoint}）能力: "
                        + "、".join(card.skills[:5]),
            remote=_run)

    # ---------- 传输层 ----------

    def _client(self):
        if self._transport is not None:
            return self._transport
        if self._http is None:
            import httpx                        # 惰性导入：不用 A2A 就不付这个依赖
            self._http = httpx.AsyncClient(timeout=self.timeout)
        return self._http

    async def _get(self, url: str) -> dict:
        resp = await self._client().get(url)
        _raise(resp)
        return resp.json()

    async def _post(self, url: str, payload: dict, card: AgentCard) -> dict:
        resp = await self._client().post(url, json=payload,
                                         headers=_auth_headers(card))
        _raise(resp)
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise A2AError(f"远程返回错误: {data['error']}")
        return data

    @staticmethod
    def _envelope(method: str, params: dict) -> dict:
        return {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                "params": params}

    async def _emit(self, type_: str, **payload) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(type_, **payload)
        except Exception:                       # noqa: BLE001 —— 事件不许拖垮调用
            pass

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# ---------- 辅助 ----------

def _collect_text(body: dict) -> tuple[str, list[str]]:
    """artifacts[].parts[].text → (正文, 产物名清单)。

    A2A 的 artifact 有两种常见形态：{"parts":[{"kind":"text","text":…}]} 与
    {"name":…, "parts":[…]}（文件型）。文本全部拼进 text，名字单独回收。
    """
    texts: list[str] = []
    names: list[str] = []
    for art in (body.get("artifacts") or []):
        if not isinstance(art, dict):
            continue
        if art.get("name"):
            names.append(str(art["name"]))
        for part in (art.get("parts") or []):
            if isinstance(part, dict) and part.get("kind") == "text":
                texts.append(str(part.get("text") or ""))
    return "\n".join(t for t in texts if t), names


def _error_text(err) -> str | None:
    if err is None:
        return None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


def _auth_headers(card: AgentCard) -> dict[str, str]:
    """认证头：只读环境变量里的令牌（密钥永不进任务书/代码/日志）。"""
    auth = card.auth or {}
    scheme = ""
    schemes = auth.get("schemes")
    if isinstance(schemes, list) and schemes:
        scheme = str(schemes[0])
    scheme = scheme or str(auth.get("type") or auth.get("scheme") or "")
    token_env = (auth.get("token_env") or auth.get("credentials")
                 or auth.get("api_key_env") or "")
    token = os.environ.get(str(token_env), "") if token_env else ""
    if token and scheme.lower() in ("bearer", "apikey", "oauth2", "oauth",
                                    "httpbearer"):
        return {"Authorization": f"Bearer {token}"}
    if token:                                   # 未声明方案：退化为 X-API-Key
        return {"X-API-Key": token}
    return {}


def _raise(resp) -> None:
    raiser = getattr(resp, "raise_for_status", None)
    if callable(raiser):
        try:
            raiser()
        except Exception as e:                  # noqa: BLE001 —— 翻译成协议错误
            raise A2AError(f"HTTP 失败: {e}") from e


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


__all__ = ["A2AClient", "A2AError", "AgentCard", "A2ATask", "TERMINAL"]
