"""core/circuit_breaker.py —— 三态电闸（M02 §1.4 / §3 难点 / §4 步骤 6）

三态：CLOSED 正常放行并统计 → OPEN 1ms 快速失败 → HALF_OPEN 放一个探测。
两大工程要点：
① 锁只保护"状态读写"（微秒级），真实调用 fn 在锁外——否则全网关串行化
② inflight 计数防"探测风暴"：探测员在途时，后续并发请求一律拒绝
本实现额外修复参考片段的坑：非 RetryableError / 取消异常也要归还探测名额。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from .errors import CircuitOpenError, RetryableError

_BUCKET = 10.0  # 统计分桶宽度（秒）：滑动窗口 = 最近 6 桶 = 60 秒
_KEEP = 6


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, *, failure_threshold: float = 0.5, min_samples: int = 20,
                 open_seconds: float = 30.0, half_open_probes: int = 1,
                 name: str = ""):
        self.failure_threshold = failure_threshold
        self.min_samples = min_samples
        self.open_seconds = open_seconds
        self.half_open_probes = half_open_probes
        self.name = name or "default"

        self._state = self.CLOSED
        self._opened_at = 0.0               # 跳闸时刻（monotonic）
        self._cooldown = open_seconds       # 当前冷却期（探测失败可翻倍）
        self._half_open_inflight = 0        # ★ 并发探测控制
        self._lock = asyncio.Lock()
        # 滑动窗口：deque[(桶起始秒, 总数, 失败数)]，10s 一桶留 6 桶
        self._buckets: deque[tuple[float, int, int]] = deque()

    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_rate(self) -> float:
        t, f = self._stats()
        return f / t if t else 0.0

    async def call(self, fn, *args, **kwargs):
        """统一过闸口：所有请求经此处包裹（装饰者，不关心业务签名）。"""
        is_probe = False
        async with self._lock:                       # ① 入口临界区（持锁）
            if self._state == self.OPEN:
                waited = time.monotonic() - self._opened_at
                if waited < self._cooldown:
                    left = self._cooldown - waited
                    raise CircuitOpenError(
                        f"[{self.name}] 熔断打开中，预计 {left:.0f} 秒后半开探测",
                        retry_after=left)
                # 冷却期到 → 懒迁移半开（无后台定时器，请求顺手完成迁移）
                self._state = self.HALF_OPEN
                self._half_open_inflight = 0
            if self._state == self.HALF_OPEN:        # 非 elif：刚迁移者继续走
                if self._half_open_inflight >= self.half_open_probes:
                    raise CircuitOpenError(f"[{self.name}] 半开探测中，稍后重试")
                self._half_open_inflight += 1         # 领取探测员资格
                is_probe = True
        try:
            result = await fn(*args, **kwargs)        # ② 执行段（不持锁！）
        except RetryableError:                        # 下游病了
            async with self._lock:
                self._record(success=False)
            raise                                     # 只监测不吞错
        except asyncio.CancelledError:                # 上层取消：归还名额后放行
            async with self._lock:
                if is_probe:
                    self._half_open_inflight -= 1
            raise
        except Exception:                             # 业务错=下游有响应=活着
            async with self._lock:
                if is_probe:                          # ★ 归还名额但不计失败
                    self._half_open_inflight -= 1
            raise
        else:
            async with self._lock:                    # ③ 出口临界区（持锁）
                self._record(success=True)
            return result

    # ---------- 记账与状态机（必须在持锁时调用）----------

    def _record(self, *, success: bool) -> None:
        if self._state == self.HALF_OPEN:
            if success:   # 探测成功 → 重新信任：闭合 + 清空全部旧账
                self._state = self.CLOSED
                self._cooldown = self.open_seconds
                self._buckets.clear()
            else:         # 探测失败 → 重新跳闸，冷却翻倍（最多 5 倍）
                self._trip(escalate=True)
            return
        # CLOSED：滑窗记账，过线跳闸
        now = time.monotonic()
        if not self._buckets or now - self._buckets[-1][0] >= _BUCKET:
            self._buckets.append((now, 0, 0))        # 开新桶
        start, total, failures = self._buckets.pop()
        self._buckets.append((start, total + 1,
                              failures + (0 if success else 1)))
        while self._buckets and now - self._buckets[0][0] > _BUCKET * _KEEP:
            self._buckets.popleft()                  # 淘汰窗外旧桶
        t, f = self._stats()
        if t >= self.min_samples and f / t >= self.failure_threshold:
            self._trip(escalate=False)

    def _trip(self, *, escalate: bool) -> None:
        self._state = self.OPEN
        self._opened_at = time.monotonic()
        self._half_open_inflight = 0
        if escalate:
            self._cooldown = min(self._cooldown * 2, self.open_seconds * 5)
        else:
            self._cooldown = self.open_seconds

    def _stats(self) -> tuple[int, int]:
        return (sum(t for _, t, _ in self._buckets),
                sum(f for *_, f in self._buckets))
