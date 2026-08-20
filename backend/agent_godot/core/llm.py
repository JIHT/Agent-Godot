"""core/llm.py —— 标准订单 + 招工启事 + 通讯录 + 总机（M02 §1.2 / §4 步骤 2）

四个角色（大白话）：
- 数据类       = 标准订单：内部所有代码只填这张单
- LLM Protocol = 招工启事：只认形状不认血缘（FakeLLM 不继承也能上岗）
- PROVIDERS    = 通讯录：适配器名 → 适配器类
- get_llm      = 总机：按 ModelConfig.provider 查通讯录实例化

主流模型接入一览（provider 字段的取值，详见 config/models.yaml）：
- provider: openai     —— USB-C 万能接头：OpenAI 官方 / DeepSeek / Qwen(DashScope
                          兼容模式) / GLM / Kimi / Gemini(OpenAI 兼容端点) /
                          vLLM / Ollama / LM Studio……一切 OpenAI 兼容端点
- provider: anthropic  —— Claude 原生协议（信封差异大，专属翻译）
- provider: lmstudio   —— 本地 LM Studio（OpenAI 兼容 + model 自动发现）
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .streaming import StreamEvent


# ---------- ① 数据类：标准订单 ----------

@dataclass
class ToolCall:
    """模型"建议"的一次函数调用。arguments 是 JSON 字符串不是对象（协议如此）。"""
    id: str
    name: str
    arguments: str


@dataclass
class Message:
    """对话便签：谁(role)说了什么(content)/发起了哪些工具调用。"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None   # assistant 发起调用时填
    tool_call_id: str | None = None            # tool 回应时的配对键

    def to_openai(self) -> dict[str, Any]:
        """普通话 → OpenAI 方言（信封翻译：结构变，内容不变）。
        Anthropic 信封差异太大，翻译在 llm_adapters.AnthropicAdapter 里。"""
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class ToolSpec:
    """FC 声明（喂给模型看的工具说明书）：M04 注册表会自动生成它。"""
    name: str
    description: str
    parameters: dict  # JSON Schema（properties / required）

    def to_openai(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


@dataclass
class ModelConfig:
    """一个"模型接入点"的配置（models.yaml providers 节的一项实例化）。

    字段解析优先级（由 config.ModelRegistry 完成，直接构造时需自己填好）：
    - api_key：显式明文（不推荐入库）
    - api_key_env：环境变量名（推荐——密钥永不进 yaml）
    - 本地服务（lmstudio/ollama）两者都缺 → 占位 key 即可
    """
    provider: str                                # 通讯录键：openai/lmstudio/anthropic
    base_url: str                                # 如 http://127.0.0.1:1234/v1
    model: str                                   # 模型名；"auto"=启动时自动发现
    api_key: str = ""                            # 已解析的密钥（明文勿入库）
    api_key_env: str | None = None               # 密钥所在环境变量名（审计/提示用）
    timeout: float = 120.0                       # 本地大模型推理慢，默认放宽
    pricing: tuple[float, float] = (0.0, 0.0)    # (输入/输出 美元每千token)
    default_headers: dict[str, str] = field(default_factory=dict)
    # 厂商特有参数透传（如 anthropic 的 thinking、openai 的 reasoning_effort）
    extra_body: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        """日志友好名：openai+gpt-4o @ https://api.openai.com/v1"""
        host = self.base_url.split("//")[-1].split("/")[0]
        return f"{self.provider}+{self.model} @ {host}"


@dataclass
class LLMRequest:
    """标准订单：内部所有代码只填这张单。"""
    model: str
    messages: list[Message]
    temperature: float = 0.3
    top_p: float = 0.95
    max_tokens: int | None = None
    tools: list[ToolSpec] | None = None
    stream: bool = True
    extra: dict | None = None      # 厂商特有参数透传通道（防抽象泄漏）


@dataclass
class Usage:
    """电表读数：本模块之后所有计量（预算/计费/配额）的数据源。"""
    input_tokens: int
    output_tokens: int
    cost_usd: float = 0.0


@dataclass
class LLMResponse:
    """非流式（complete）的返回。"""
    content: str
    tool_calls: list[ToolCall]
    usage: Usage | None
    finish_reason: str | None


# ---------- ② 招工启事：Protocol ----------

class LLM(Protocol):
    """会接单(complete)/会流式发货(stream)/会称重(count_tokens)即可上岗，
    不看门派出身——测试塞个 FakeLLM（不继承任何人）mypy 也放行。"""

    async def complete(self, req: LLMRequest) -> LLMResponse: ...
    def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]: ...
    def count_tokens(self, messages: list[Message]) -> int: ...


# ---------- ③ 通讯录：注册表 ----------

PROVIDERS: dict[str, type] = {}


def register_provider(name: str) -> Callable[[type], type]:
    """前台入职登记（装饰器）：把类写进通讯录，类本身原样返还。
    "新增 Provider 零改动核心"的全部机关——M04 工具注册表同款。"""
    def deco(cls: type) -> type:
        PROVIDERS[name] = cls
        return cls
    return deco


# ---------- ④ 总机：工厂 ----------

def get_llm(config: ModelConfig, *, cache=None) -> LLM:
    """按 config.provider 查通讯录实例化适配器。

    兼容两种用法：
    - 直接构造：get_llm(ModelConfig(provider="lmstudio", ...))
    - 配置驱动（推荐）：registry.llm("lmstudio/auto") / registry.llm_for_mode("craft")
    """
    from . import llm_adapters  # noqa: F401  导入即触发前台注册（入职副作用）
    cls = PROVIDERS.get(config.provider)
    if cls is None:
        raise ValueError(f"未注册的 provider: {config.provider!r}，"
                         f"已注册: {sorted(PROVIDERS)}")
    return cls(config, cache=cache)  # type: ignore[call-arg]
