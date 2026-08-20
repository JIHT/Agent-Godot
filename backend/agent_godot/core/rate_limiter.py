"""core/rate_limiter.py —— 客户端令牌桶（M02 §1.6 / §4 步骤 7）

大白话：匀速滴水的水桶。平时随便舀（burst 突发额度），见底了排队等水。
分工：限流保护"自己不被服务端封"（熔断才是保护"不被下游拖死"）。

要点：惰性计算（取用时才按流逝时间补令牌，无需后台协程）；
计时用 monotonic（墙钟被 NTP 回拨会让桶"凭空回血"）。
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate: float, burst: int, name: str = ""):
        """rate：每秒补充令牌数；burst：桶容量（允许的瞬时突发）。"""
        self.rate = float(rate)
        self.burst = float(burst)
        self.name = name or "default"
        self._tokens = float(burst)   # 初始满桶
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> float:
        """取 n 个令牌。不够时睡眠等待，返回实际等待秒数。"""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                # 惰性补充：流逝的每一秒补 rate 个，封顶 burst
                self._tokens = min(self.burst,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return waited
                deficit = n - self._tokens
            # 亏多少等多久；锁外睡眠（持锁睡眠=串行化所有等待者）
            wait = deficit / self.rate
            await asyncio.sleep(wait)
            waited += wait
