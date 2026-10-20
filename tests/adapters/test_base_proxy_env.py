"""``NO_PROXY`` 方括号 IPv6 写法不得让 httpx 客户端构造失败。"""

from __future__ import annotations

import httpx
import pytest

from shopkeeper.adapters.base import _build_http_client, safe_proxy_env


def _restore(monkeypatch: pytest.MonkeyPatch, env: dict[str, str | None]) -> None:
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_bracketed_ipv6_no_proxy_no_longer_breaks_client(monkeypatch):
    """回归：``NO_PROXY`` 同时含 ``::1`` 与 ``[::1]`` 时，构造客户端不能抛异常。

    httpx 0.28 把 ``[::1]`` 当成带端口的主机，解析端口 ``:1]`` 失败并抛
    ``InvalidURL``。项目里 10 个适配器都在构造客户端，一处环境变量写法就会
    让整条链路起不来。
    """
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    client = _build_http_client(timeout=5.0)
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_raw_httpx_still_fails_without_normalization(monkeypatch):
    """反向断言：没有规范化时 httpx 确实会崩，证明上面的测试不是空转。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    with pytest.raises(httpx.InvalidURL):
        httpx.Client(timeout=5.0)


def test_safe_proxy_env_strips_brackets_and_dedupes(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    env = safe_proxy_env()
    assert env is not None
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert env["no_proxy"] == "localhost,127.0.0.1,::1"


def test_safe_proxy_env_returns_none_when_nothing_to_fix(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    assert safe_proxy_env() is None


def test_build_http_client_restores_process_env(monkeypatch):
    """构造期间的改写必须还原，不能泄漏到进程环境。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    client = _build_http_client(timeout=5.0)
    client.close()
    import os

    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1,[::1]"
    assert os.environ["no_proxy"] == "localhost,127.0.0.1,::1,[::1]"


def test_build_http_client_without_no_proxy(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    client = _build_http_client(timeout=5.0)
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()
