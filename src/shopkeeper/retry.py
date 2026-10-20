"""统一外部调用重试（DESIGN §2.2 / §3.3）。

边界：
- **生成类**（``ErrorKind.GENERATION`` / ``retryable=True``）：指数退避 + 抖动，
  上限可配（默认 2 次重试 = 共 3 次尝试），耗尽返回最后一次失败。
- **执行 / 校验 / 策略类**：永不自动重试（防重复上架/下单）。

退避公式：``min(max, base * 2^(attempt-1))``，再按 ``jitter_ratio`` 做 full jitter
（``delay * U(1-jitter, 1)`` 的等价形式：``delay * ((1-j) + j*U(0,1))``）。
环境变量：``RETRY_MAX_RETRIES`` / ``RETRY_BACKOFF_BASE_S`` /
``RETRY_BACKOFF_MAX_S`` / ``RETRY_JITTER_RATIO``。
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Callable, TypeVar

from shopkeeper.errors import ErrorKind
from shopkeeper.models import AdapterResult

T = TypeVar("T")

GENERATION_MAX_RETRIES = 2  # = 最多 3 次尝试
DEFAULT_BACKOFF_BASE_S = 0.05
DEFAULT_BACKOFF_MAX_S = 2.0
DEFAULT_JITTER_RATIO = 1.0  # full jitter


@dataclass(frozen=True)
class RetryConfig:
    """可配置重试上限与退避。执行类写操作不得用本配置自动重试副作用。"""

    max_retries: int = GENERATION_MAX_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S
    jitter_ratio: float = DEFAULT_JITTER_RATIO

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_retries", max(0, int(self.max_retries)))
        object.__setattr__(self, "backoff_base_s", max(0.0, float(self.backoff_base_s)))
        object.__setattr__(
            self,
            "backoff_max_s",
            max(float(self.backoff_base_s), float(self.backoff_max_s)),
        )
        ratio = float(self.jitter_ratio)
        if ratio < 0.0:
            ratio = 0.0
        if ratio > 1.0:
            ratio = 1.0
        object.__setattr__(self, "jitter_ratio", ratio)


def retry_config_from_env(env: dict[str, str] | None = None) -> RetryConfig:
    """从环境读取重试配置；缺省与非法值回落默认，不抛。"""
    source = env if env is not None else os.environ

    def _int(name: str, default: int) -> int:
        raw = (source.get(name) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        raw = (source.get(name) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return RetryConfig(
        max_retries=_int("RETRY_MAX_RETRIES", GENERATION_MAX_RETRIES),
        backoff_base_s=_float("RETRY_BACKOFF_BASE_S", DEFAULT_BACKOFF_BASE_S),
        backoff_max_s=_float("RETRY_BACKOFF_MAX_S", DEFAULT_BACKOFF_MAX_S),
        jitter_ratio=_float("RETRY_JITTER_RATIO", DEFAULT_JITTER_RATIO),
    )


def compute_backoff(
    attempt: int,
    *,
    base_s: float = DEFAULT_BACKOFF_BASE_S,
    max_s: float = DEFAULT_BACKOFF_MAX_S,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    rng: random.Random | None = None,
) -> float:
    """第 ``attempt`` 次失败后的等待秒数（attempt 从 1 起）。

    ``attempt=1`` → base；随后指数增长，封顶 max，再套抖动。
    """
    n = max(1, int(attempt))
    delay = min(float(max_s), float(base_s) * (2 ** (n - 1)))
    if delay <= 0.0:
        return 0.0
    j = float(jitter_ratio)
    if j <= 0.0:
        return delay
    if j > 1.0:
        j = 1.0
    roller = rng.random() if rng is not None else random.random()
    return delay * ((1.0 - j) + j * roller)


def is_retryable_error(result: AdapterResult[T]) -> bool:
    """可重试判定：成功不可重试；否则看 error.retryable 或 GENERATION kind。"""
    if result.ok:
        return False
    err = result.error
    if err is None:
        return False
    if err.retryable:
        return True
    return err.kind == ErrorKind.GENERATION


def call_with_retry(
    fn: Callable[[], AdapterResult[T]],
    *,
    config: RetryConfig | None = None,
    on_retry: Callable[[int, AdapterResult[T], float], None] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    is_retryable: Callable[[AdapterResult[T]], bool] | None = None,
    rng: random.Random | None = None,
) -> tuple[AdapterResult[T], int]:
    """统一重试入口。返回 ``(最终结果, 实际尝试次数)``。

    - 成功 → 立即返回
    - 可重试错误 → 退避后重试，最多 ``config.max_retries`` 次
    - 不可重试 → 立刻返回，不 sleep
    """
    cfg = config or RetryConfig()
    decide = is_retryable or is_retryable_error
    attempts = 0
    last: AdapterResult[T] | None = None
    while attempts <= cfg.max_retries:
        attempts += 1
        last = fn()
        if last.ok:
            return last, attempts
        if not decide(last):
            return last, attempts
        if attempts > cfg.max_retries:
            break
        delay = compute_backoff(
            attempts,
            base_s=cfg.backoff_base_s,
            max_s=cfg.backoff_max_s,
            jitter_ratio=cfg.jitter_ratio,
            rng=rng,
        )
        if on_retry is not None:
            on_retry(attempts, last, delay)
        if sleep_fn is not None and delay > 0.0:
            sleep_fn(delay)
    assert last is not None
    return last, attempts


def call_generation(
    fn: Callable[[], AdapterResult[T]],
    *,
    max_retries: int = GENERATION_MAX_RETRIES,
    on_retry: Callable[[int, AdapterResult[T]], None] | None = None,
    config: RetryConfig | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> tuple[AdapterResult[T], int]:
    """生成类重试（兼容旧签名）。

    - 成功 → 立即返回
    - GENERATION 类错误 → 重试，最多 max_retries 次；耗尽后返回最后一次失败
    - EXECUTION / VALIDATION / POLICY → **不重试**，直接返回
    """
    cfg = config or RetryConfig(max_retries=max_retries)

    def _bridge(attempt: int, last: AdapterResult[T], _delay: float) -> None:
        if on_retry is not None:
            on_retry(attempt, last)

    return call_with_retry(
        fn,
        config=cfg,
        on_retry=_bridge if on_retry is not None else None,
        sleep_fn=sleep_fn,
        is_retryable=lambda r: (r.error.kind == ErrorKind.GENERATION) if r.error else False,
        rng=rng,
    )
