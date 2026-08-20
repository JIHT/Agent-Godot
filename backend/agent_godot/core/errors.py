"""core/errors.py —— 错误分类树（M02 §1.1 / §4 步骤 1）

设计原则："错误也是数据"：
- 可重试（429/5xx/超时/断网）→ RetryableError，交给 retry.py 错峰重敲
- 不可重试（400/401）→ 直接抛给上层修请求/换 key，重试无意义
- 熔断打开（CircuitOpenError）→ 1ms 快速失败，由网关降级路由接住
"""

from __future__ import annotations


class LLMError(Exception):
    """全家族祖先：上层一个 `except LLMError` 一网打尽。"""

    def __init__(self, message: str, *, provider: str | None = None):
        super().__init__(message)
        self.provider = provider  # 出错的是哪家厂商（日志/审计用）


class RetryableError(LLMError):
    """可重试：429 限流 / 5xx 服务端故障 / 网络超时。

    retry_after：服务端明示的等待秒数（429 的 Retry-After 头）。
    有值时 retry.py 优先尊重它，而不是自算指数退避。
    """

    def __init__(self, message: str, *, retry_after: float | None = None,
                 provider: str | None = None):
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


class RateLimitError(RetryableError):
    """429 专属。为什么单独一个类：熔断统计要区分"限流"（不算服务挂了）
    和"服务器炸了"（计入失败率）——同一个父类，两种账。"""


class AuthError(LLMError):
    """401/403：key 错/没权限——重试一万次也不会好，该换 key。"""


class BadRequestError(LLMError):
    """400/404/422：请求本身错（参数/格式）——该改代码，不该重试。"""


class CircuitOpenError(LLMError):
    """熔断打开：下游已判定死亡，1ms 快速失败。

    retry_after = 距离下次半开探测的秒数。
    注意：故意不继承 RetryableError——with_retry 不应重试它
    （重试会浪费熔断"快速失败"的价值），应直接上抛给降级路由。
    """

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def classify(status: int, headers: dict[str, str] | None = None,
             body: str = "", *, provider: str | None = None) -> LLMError:
    """HTTP 状态码 → 异常类的翻译函数（Adapter 每次非 2xx 响应都调它）。"""
    headers = headers or {}
    if status == 429:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            wait = float(raw) if raw is not None else None
        except ValueError:
            wait = None  # HTTP-date 格式（少见），放弃解析降级为 None
        return RateLimitError(f"429 限流: {body[:200]}",
                              retry_after=wait, provider=provider)
    if status >= 500:
        return RetryableError(f"{status} 服务端错误: {body[:200]}",
                              provider=provider)
    if status in (401, 403):
        return AuthError(f"{status} 鉴权失败: {body[:200]}", provider=provider)
    return BadRequestError(f"{status} 请求错误: {body[:200]}", provider=provider)
