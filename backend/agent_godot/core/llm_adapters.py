"""core/llm_adapters.py —— 各家厂商的前台翻译（M02 §1.2 / §4 步骤 4）

大白话三件套：数据类=标准订单，Adapter=各家前台（订单誊成他们家的表格），
注册表=通讯录。本文件给三家前台办入职：

- OpenAIAdapter    ★万能接头：OpenAI 官方 / DeepSeek / Qwen / GLM / Kimi /
                   Gemini(兼容端点) / vLLM / Ollama……一切 OpenAI 兼容端点
                   ——差别只在 base_url 与 key（models.yaml 里配），代码同一份
- LMStudioAdapter  本地 LM Studio（OpenAI 兼容 → 直接继承；补 model 自动发现）
- AnthropicAdapter 信封差异最大：system 顶层字段 / tool_use 命名 / 流事件多类

职责边界：Adapter 只做"翻译"；重试与熔断是独立的韧性管道，在 _send 组装。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .circuit_breaker import CircuitBreaker
from .errors import RetryableError, classify
from .llm import (LLMRequest, LLMResponse, Message, ModelConfig, ToolCall,
                  Usage, register_provider)
from .retry import with_retry
from .semantic_cache import SemanticCache
from .streaming import DONE, StreamAggregator, StreamEvent, parse_sse_line


@register_provider("openai")
class OpenAIAdapter:
    """OpenAI 兼容系前台：一个类服务全部兼容厂商（models.yaml 配置驱动）。"""

    def __init__(self, config: ModelConfig, cache: SemanticCache | None = None):
        self.config = config
        self._cache = cache
        headers = {"Authorization": f"Bearer {config.api_key or 'lm-studio'}"}
        headers.update(config.default_headers)  # 厂商特有头（如 OpenRouter 的渠道头）
        self._client = httpx.AsyncClient(timeout=config.timeout, headers=headers)
        # 韧性管道：真实请求 = breaker.call( retry( _post_sse ) )
        # breaker 只包"建连+状态码"段——流的中途断开不重放（协议无断点续传）
        self.breaker = CircuitBreaker(name=f"{config.provider}:{config.model}")
        self._send = with_retry(self._post_sse)

    # ---------- 协议三方法 ----------

    def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        # 语义缓存：只对"纯问答 + 无工具副作用"开放（红线③：tools 存在不查）
        if self._cache is not None and not req.tools:
            if (hit := self._cache.get(req.messages)) is not None:
                yield StreamEvent(type="text_delta", text=hit)
                yield StreamEvent(type="done", finish_reason="stop")
                return

        aggregator = StreamAggregator()
        resp = await self.breaker.call(self._send, self._build_payload(req))
        try:
            async for line in resp.aiter_lines():
                parsed = parse_sse_line(line)
                if parsed is None or parsed is DONE:  # 空行/心跳/[DONE] 跳过
                    continue
                for ev in aggregator.feed(parsed):
                    yield ev
        except httpx.TransportError as e:  # 流中断：整次失败上抛
            raise RetryableError(f"流中断: {e}",
                                 provider=self.config.provider) from e
        finally:
            await resp.aclose()

        if (self._cache is not None and not req.tools
                and aggregator.finish_reason == "stop" and aggregator.text):
            self._cache.put(req.messages, aggregator.text)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """非流式 = 流式的整段消费（接口完整起见；主路径是 stream）。"""
        parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: Usage | None = None
        finish: str | None = None
        async for ev in self.stream(req):
            if ev.type == "text_delta" and ev.text:
                parts.append(ev.text)
            elif ev.type == "usage":
                usage = ev.usage
            elif ev.type == "done":
                finish = ev.finish_reason
                tool_calls = ev.tool_calls or []
        return LLMResponse(content="".join(parts), tool_calls=tool_calls,
                           usage=usage, finish_reason=finish)

    def count_tokens(self, messages: list[Message]) -> int:
        """粗估：中文≈0.6 token/字、英文≈0.25/字符、每条+4 结构开销。
        M07 的 TokenCounter 会给双模+自校准正式版，此处先占位。"""
        total = 2
        for m in messages:
            total += 4
            text = m.content or ""
            cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
            total += int(cjk * 0.6 + (len(text) - cjk) / 4)
        return total

    # ---------- 信封翻译（出站：普通话 → OpenAI 方言）----------

    def _build_payload(self, req: LLMRequest) -> dict:
        payload: dict = {
            "model": self.config.model if req.model == "auto" else req.model,
            "messages": [m.to_openai() for m in req.messages],
            "temperature": req.temperature,
            "top_p": req.top_p,
            "stream": True,
            "stream_options": {"include_usage": True},  # ★ 结账开关
        }
        if self.config.extra_body:  # 厂商特有参数（models.yaml extra_body 节）
            payload.update(self.config.extra_body)
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.tools:
            payload["tools"] = [t.to_openai() for t in req.tools]
        if req.extra:  # 单次请求级透传（防抽象泄漏）
            payload.update(req.extra)
        return payload

    async def _post_sse(self, payload: dict) -> httpx.Response:
        """真实网络往返（被 retry 包裹）：POST → 验状态码 → 返回响应流。"""
        try:
            resp = await self._client.post(
                f"{self.config.base_url}/chat/completions", json=payload)
        except httpx.TransportError as e:  # 超时/拒连/断网 → 可重试类
            raise RetryableError(f"网络错误: {e}",
                                 provider=self.config.provider) from e
        if resp.status_code != 200:
            raise classify(resp.status_code, dict(resp.headers),
                           resp.text[:500], provider=self.config.provider)
        return resp

    async def aclose(self) -> None:
        await self._client.aclose()


@register_provider("lmstudio")
class LMStudioAdapter(OpenAIAdapter):
    """本地 LM Studio 前台：OpenAI 兼容 → 直接继承。

    两个本地特化：
    - 默认地址与占位 key（LM Studio 不校验鉴权）
    - model: auto 支持：首次使用时查 /v1/models 自动解析已加载模型
      ——彻底告别"模型名不匹配"的坑
    """

    def __init__(self, config: ModelConfig, cache: SemanticCache | None = None):
        if not config.base_url:
            config.base_url = "http://127.0.0.1:1234/v1"
        if not config.api_key:
            config.api_key = "lm-studio"  # 本地不校验，占位即可
        super().__init__(config, cache)
        self._resolved_model: str | None = None  # auto 解析结果缓存

    async def _resolve_model(self) -> None:
        """model=auto 时查 /v1/models 取第一个已加载模型（只查一次，结果缓存）。"""
        if self._resolved_model or self.config.model != "auto":
            return
        try:
            resp = await self._client.get(f"{self.config.base_url}/models")
            resp.raise_for_status()
            models = [m["id"] for m in resp.json().get("data", [])]
        except Exception as e:  # 发现失败给明确指引（你踩过的坑）
            raise RuntimeError(
                f"LM Studio 模型发现失败（GET {self.config.base_url}/models）: {e}\n"
                f"请确认 LM Studio 已启动且已加载模型；"
                f"或在 models.yaml 里写死具体模型名") from e
        if not models:
            raise RuntimeError("LM Studio 已启动但未加载任何模型——"
                               "请先在 LM Studio 里加载一个模型")
        self._resolved_model = models[0]
        # 同步回填 config，让熔断器命名/日志/计费都拿到真实模型名
        self.config.model = self._resolved_model
        self.breaker.name = f"lmstudio:{self._resolved_model}"

    async def _stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        await self._resolve_model()  # 首帧前确保模型名已解析
        if req.model == "auto":
            req = LLMRequest(  # 请求对象不可变语义：替换 model 后下传
                model=self._resolved_model, messages=req.messages,
                temperature=req.temperature, top_p=req.top_p,
                max_tokens=req.max_tokens, tools=req.tools,
                stream=req.stream, extra=req.extra)
        async for ev in super()._stream(req):
            yield ev


@register_provider("anthropic")
class AnthropicAdapter:
    """信封差异最大的前台：system 顶层字段 / tool_use 命名 / 流事件多类。"""

    def __init__(self, config: ModelConfig, cache: SemanticCache | None = None):
        self.config = config
        self._cache = cache
        headers = {"x-api-key": config.api_key,
                   "anthropic-version": "2023-06-01"}
        headers.update(config.default_headers)
        self._client = httpx.AsyncClient(timeout=config.timeout, headers=headers)
        self.breaker = CircuitBreaker(name=f"anthropic:{config.model}")
        self._send = with_retry(self._post_sse)

    # ---------- 信封翻译（M02 §1.2 ③ 的完整版）----------

    def _translate_messages(self, messages: list[Message]) -> dict:
        """普通话 → Anthropic 方言。★ 翻译的是信封，信（content）一字不动。"""
        system = "\n".join(m.content for m in messages if m.role == "system")
        turns = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "user":
                turns.append({"role": "user", "content": m.content or ""})
            elif m.role == "assistant":
                if m.tool_calls:  # 调用 → tool_use 块
                    blocks = []
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    for tc in m.tool_calls:
                        blocks.append({"type": "tool_use", "id": tc.id,
                                       "name": tc.name,
                                       "input": json.loads(tc.arguments)})
                    turns.append({"role": "assistant", "content": blocks})
                else:
                    turns.append({"role": "assistant", "content": m.content or ""})
            elif m.role == "tool":  # 工具结果 → user 角色的 tool_result 块
                turns.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": m.tool_call_id,
                     "content": m.content or ""}]})
        return {"system": system, "messages": turns}

    def _build_payload(self, req: LLMRequest) -> dict:
        env = self._translate_messages(req.messages)
        payload: dict = {"model": self.config.model if req.model == "auto"
                         else req.model,
                         **env, "max_tokens": req.max_tokens or 4096,
                         "temperature": req.temperature, "stream": True}
        if req.tools:
            payload["tools"] = [{"name": t.name, "description": t.description,
                                 "input_schema": t.parameters} for t in req.tools]
        if self.config.extra_body:
            payload.update(self.config.extra_body)
        if req.extra:
            payload.update(req.extra)
        return payload

    async def _post_sse(self, payload: dict) -> httpx.Response:
        try:
            resp = await self._client.post(f"{self.config.base_url}/messages",
                                           json=payload)
        except httpx.TransportError as e:
            raise RetryableError(f"网络错误: {e}",
                                 provider=self.config.provider) from e
        if resp.status_code != 200:
            raise classify(resp.status_code, dict(resp.headers),
                           resp.text[:500], provider=self.config.provider)
        return resp

    # ---------- 流事件翻译（入站：Anthropic 方言 → 统一事件）----------

    def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        aggregator = StreamAggregator()
        resp = await self.breaker.call(self._send, self._build_payload(req))
        try:
            async for line in resp.aiter_lines():
                parsed = parse_sse_line(line)
                if not isinstance(parsed, dict):
                    continue
                for ev in self._translate_event(parsed, aggregator):
                    yield ev
        finally:
            await resp.aclose()

    def _translate_event(self, chunk: dict, agg: StreamAggregator
                         ) -> list[StreamEvent]:
        """把 Anthropic 流事件合成为 OpenAI 形状的 chunk 喂给共享聚合器
        ——入站翻译复用同一台拼图机，这是"适配器模式"的漂亮收口。"""
        events: list[StreamEvent] = []
        etype = chunk.get("type")
        if etype == "content_block_start":
            block = chunk.get("content_block", {})
            if block.get("type") == "tool_use":  # 工具调用开始（id/name 到货）
                frag = {"index": chunk.get("index", 0), "id": block.get("id"),
                        "function": {"name": block.get("name"), "arguments": ""}}
                events += agg.feed({"choices": [{"delta": {"tool_calls": [frag]}}]})
        elif etype == "content_block_delta":
            d = chunk.get("delta", {})
            if piece := d.get("text"):           # 文本增量
                agg.text += piece
                events.append(StreamEvent(type="text_delta", text=piece))
            if pj := d.get("partial_json"):      # 工具参数分片
                frag = {"index": chunk.get("index", 0),
                        "function": {"arguments": pj}}
                events += agg.feed({"choices": [{"delta": {"tool_calls": [frag]}}]})
        elif etype == "message_delta":
            if usage := chunk.get("usage"):      # 账单（Anthropic 在尾部给）
                agg.usage = Usage(usage.get("input_tokens", 0),
                                  usage.get("output_tokens", 0))
                events.append(StreamEvent(type="usage", usage=agg.usage))
        elif etype == "message_stop":
            if agg._buffers:
                agg._finalize_tool_calls()
            events.append(StreamEvent(
                type="done",
                finish_reason="tool_calls" if agg.tool_calls else "stop",
                tool_calls=agg.tool_calls or None))
        return events

    async def complete(self, req: LLMRequest) -> LLMResponse:
        parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: Usage | None = None
        finish: str | None = None
        async for ev in self.stream(req):
            if ev.type == "text_delta" and ev.text:
                parts.append(ev.text)
            elif ev.type == "usage":
                usage = ev.usage
            elif ev.type == "done":
                finish = ev.finish_reason
                tool_calls = ev.tool_calls or []
        return LLMResponse(content="".join(parts), tool_calls=tool_calls,
                           usage=usage, finish_reason=finish)

    def count_tokens(self, messages: list[Message]) -> int:
        total = 2
        for m in messages:
            total += 4
            text = m.content or ""
            cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
            total += int(cjk * 0.6 + (len(text) - cjk) / 4)
        return total

    async def aclose(self) -> None:
        await self._client.aclose()
