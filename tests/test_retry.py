"""统一重试：指数退避+抖动、仅可重试错误、上限可配。"""

from __future__ import annotations

import random

from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import AdapterResult
from shopkeeper.retry import (
    RetryConfig,
    call_generation,
    call_with_retry,
    compute_backoff,
    is_retryable_error,
    retry_config_from_env,
)


def test_compute_backoff_exponential_and_cap() -> None:
    rng = random.Random(0)
    d1 = compute_backoff(1, base_s=0.1, max_s=1.0, jitter_ratio=0.0, rng=rng)
    d2 = compute_backoff(2, base_s=0.1, max_s=1.0, jitter_ratio=0.0, rng=rng)
    d3 = compute_backoff(3, base_s=0.1, max_s=1.0, jitter_ratio=0.0, rng=rng)
    d10 = compute_backoff(10, base_s=0.1, max_s=1.0, jitter_ratio=0.0, rng=rng)
    assert d1 == 0.1
    assert d2 == 0.2
    assert d3 == 0.4
    assert d10 == 1.0


def test_compute_backoff_full_jitter_in_range() -> None:
    rng = random.Random(42)
    samples = [
        compute_backoff(3, base_s=1.0, max_s=10.0, jitter_ratio=1.0, rng=rng)
        for _ in range(20)
    ]
    assert all(0.0 <= s <= 4.0 for s in samples)
    assert min(samples) < max(samples)


def test_retry_config_from_env_and_defaults() -> None:
    cfg = retry_config_from_env(
        {
            "RETRY_MAX_RETRIES": "1",
            "RETRY_BACKOFF_BASE_S": "0.2",
            "RETRY_BACKOFF_MAX_S": "1.5",
            "RETRY_JITTER_RATIO": "0.5",
        }
    )
    assert cfg.max_retries == 1
    assert cfg.backoff_base_s == 0.2
    assert cfg.backoff_max_s == 1.5
    assert cfg.jitter_ratio == 0.5
    bad = retry_config_from_env({"RETRY_MAX_RETRIES": "nope", "RETRY_JITTER_RATIO": "9"})
    assert bad.max_retries == 2
    assert bad.jitter_ratio == 1.0


def test_call_with_retry_only_retryable_errors() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def boom() -> AdapterResult[dict]:
        calls["n"] += 1
        return AdapterResult.failure(
            AdapterError.execution("TTS_HTTP_ERROR", "no retry"),
            "t",
        )

    res, attempts = call_with_retry(
        boom,
        config=RetryConfig(max_retries=3, jitter_ratio=0.0),
        sleep_fn=sleeps.append,
        rng=random.Random(0),
    )
    assert res.ok is False
    assert attempts == 1
    assert calls["n"] == 1
    assert sleeps == []


def test_call_with_retry_generation_with_backoff() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky() -> AdapterResult[dict]:
        calls["n"] += 1
        if calls["n"] < 3:
            return AdapterResult.failure(
                AdapterError.generation("LLM_TIMEOUT", "slow"),
                "t",
            )
        return AdapterResult.success({"ok": True}, "t")

    res, attempts = call_with_retry(
        flaky,
        config=RetryConfig(max_retries=2, backoff_base_s=0.1, backoff_max_s=1.0, jitter_ratio=0.0),
        sleep_fn=sleeps.append,
        rng=random.Random(0),
    )
    assert res.ok is True
    assert attempts == 3
    assert sleeps == [0.1, 0.2]


def test_call_with_retry_exhausts_budget() -> None:
    calls = {"n": 0}

    def always() -> AdapterResult[dict]:
        calls["n"] += 1
        return AdapterResult.failure(
            AdapterError.generation("A1688_TIMEOUT", "timeout"),
            "t",
        )

    res, attempts = call_with_retry(
        always,
        config=RetryConfig(max_retries=2, jitter_ratio=0.0),
        sleep_fn=lambda _s: None,
        rng=random.Random(0),
    )
    assert res.ok is False
    assert attempts == 3
    assert calls["n"] == 3
    assert res.error is not None
    assert res.error.code == "A1688_TIMEOUT"


def test_call_generation_compat_stops_on_policy() -> None:
    calls = {"n": 0}

    def policy() -> AdapterResult[dict]:
        calls["n"] += 1
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", "blocked"),
            "t",
        )

    res, attempts = call_generation(policy, max_retries=2)
    assert attempts == 1
    assert calls["n"] == 1
    assert res.error is not None
    assert res.error.kind == ErrorKind.POLICY


def test_is_retryable_error_helper() -> None:
    ok = AdapterResult.success({}, "t")
    assert is_retryable_error(ok) is False
    gen = AdapterResult.failure(AdapterError.generation("LLM_TIMEOUT", "x"), "t")
    assert is_retryable_error(gen) is True
    exe = AdapterResult.failure(AdapterError.execution("TTS_HTTP_ERROR", "x"), "t")
    assert is_retryable_error(exe) is False
