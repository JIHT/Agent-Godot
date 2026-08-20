"""core/retry.py —— 指数退避 + 抖动（M02 §1.3 / §4 步骤 5）

大白话：敲门没人应 → 等一会儿再敲，越敲不动等得越久，每次时长随机
（防"惊群共振"：故障恢复瞬间所有客户端同一毫秒一起重试）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .errors import RetryableError

T = TypeVar("T")


def with_retry(fn: Callable[..., Awaitable[T]], *, max_retries: int = 3,
               base: float = 0.5, cap: float = 30.0) -> Callable[..., Awaitable[T]]:
    """给异步函数 fn 包一层重试。

    规则：
    - 只重试 RetryableError（429/5xx/超时）；AuthError/BadRequest 该炸就炸
    - 429 的 retry_after（服务端明示）优先于自算退避
    - 退避 = random(0, min(base×2ⁿ, cap))：full jitter
    """
    async def wrapped(*args, **kwargs) -> T:
        for attempt in range(max_retries + 1):  # 首次 + 最多 max_retries 次重试
            try:
                return await fn(*args, **kwargs)
            except RetryableError as e:
                if attempt >= max_retries:      # 额度用尽：原样上抛
                    raise
                if e.retry_after is not None:
                    delay = min(e.retry_after, cap)  # 服务端明示优先
                else:
                    delay = random.uniform(0, min(base * 2 ** attempt, cap))
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")
    return wrapped
