"""core：模型接入地基（M02）—— 协议、适配器、韧性管道、配置注册中心。"""
from .config import ModelRegistry, load_registry
from .errors import (AuthError, BadRequestError, CircuitOpenError, LLMError,
                     RateLimitError, RetryableError, classify)
from .llm import (LLM, LLMRequest, LLMResponse, Message, ModelConfig,
                  ToolCall, ToolSpec, Usage, get_llm, register_provider)
from .streaming import DONE, StreamAggregator, StreamEvent, parse_sse_line

__all__ = [
    "ModelRegistry", "load_registry",
    "AuthError", "BadRequestError", "CircuitOpenError", "LLMError",
    "RateLimitError", "RetryableError", "classify",
    "LLM", "LLMRequest", "LLMResponse", "Message", "ModelConfig",
    "ToolCall", "ToolSpec", "Usage", "get_llm", "register_provider",
    "DONE", "StreamAggregator", "StreamEvent", "parse_sse_line",
]
