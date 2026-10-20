"""浏览器冒烟：/workflow 无 console error，状态可读，mock 上架后商品库有行。

需要 playwright；未安装或未装浏览器时 skip。CI 在 pip 后执行
`python -m playwright install chromium`。

**超时为什么要可配**（P1-12）：2026-10-14 的 Windows CI 上本文件在
`wait_for_selector("button[data-act='approve']")` 处 15s 超时而失败，**同一 commit
重跑即通过** —— 典型的「机器慢一点就红」。15s 对本地足够，但 Windows runner 上
冷启动 Chromium 再跑一轮 generate→approve 往返偏紧。默认放宽到 30s，并允许用
`SMOKE_TIMEOUT_MS` 覆盖（排查时可调小以复现）。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from shopkeeper.workflow.serve import create_runtime_app
from tests.workflow.env_isolation import isolate_runtime_env

playwright = pytest.importorskip("playwright.sync_api")

#: 单个 UI 等待的上限。默认 30s（原 15s 在慢 runner 上会假红）。
SMOKE_TIMEOUT_MS = int(os.environ.get("SMOKE_TIMEOUT_MS") or 30_000)
#: 服务端就绪等待；与 UI 无关，单独一个上限。
SERVER_START_TIMEOUT_S = 15


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def workflow_base_url(tmp_path: Path, monkeypatch):
    isolate_runtime_env(monkeypatch, tmp_path / "browser", platform="mock")
    app = create_runtime_app(start_scheduler=False)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="workflow-smoke-uvicorn", daemon=True)
    thread.start()
    deadline = time.time() + SERVER_START_TIMEOUT_S
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("uvicorn 未在时限内启动")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _wait_or_explain(page, selector: str, *, step: str) -> None:
    """等选择器；超时时把页面当时的可见文本与 console error 一起抛出来。

    为什么需要：CI 上只看到 `TimeoutError: wait_for_selector` 无法判断是「后端慢」
    还是「前端报错没渲染按钮」。P1-12 那次 Windows 失败就是只有一行超时，重跑又过。
    """
    try:
        page.wait_for_selector(selector, timeout=SMOKE_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        try:
            body = (page.locator("body").inner_text() or "")[:1200]
        except Exception:  # noqa: BLE001
            body = "<无法读取页面文本>"
        raise AssertionError(
            f"[{step}] 等不到 {selector}（上限 {SMOKE_TIMEOUT_MS}ms）。\n"
            f"页面当时可见文本：\n{body}"
        ) from exc


def test_workflow_page_smoke_generate_publish_catalog(workflow_base_url: str):
    console_errors: list[str] = []
    try:
        pw_cm = playwright.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright 不可用：{exc}")
    try:
        try:
            browser = pw_cm.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"playwright chromium 不可用：{exc}")
        page = browser.new_page()
        page.on(
            "console",
            lambda msg: console_errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None,
        )
        page.on("pageerror", lambda err: console_errors.append(f"pageerror: {err}"))

        page.goto(f"{workflow_base_url}/workflow", wait_until="networkidle")
        status = page.locator("#status")
        status.wait_for(state="visible")
        page.wait_for_function(
            "() => { const t = document.getElementById('status')?.textContent || ''; "
            "return t && !t.includes('正在读取状态'); }"
        )
        assert "正在读取状态" not in status.inner_text()
        assert "TikTok" in status.inner_text()

        page.fill("#name", "冒烟分隔盒")
        page.fill("#specs", "外径 20cm，材质 PP")
        page.select_option("#kind", "listing")
        page.click("#generate")
        # 超时失败时给出「页面当时是什么样」，否则只看到 TimeoutError 无从下手。
        _wait_or_explain(page, "button[data-act='approve']", step="generate→approve")
        page.click("button[data-act='approve']")
        _wait_or_explain(page, "button[data-act='publish']", step="approve→publish")
        page.click("button[data-act='publish']")
        page.wait_for_function(
            "() => { const t = document.getElementById('catalog-items')?.innerText || ''; "
            "return t.includes('冒烟分隔盒') || t.includes('mock-pp-'); }",
            timeout=SMOKE_TIMEOUT_MS,
        )
        catalog = page.locator("#catalog-items").inner_text()
        assert "mock-pp-" in catalog or "冒烟分隔盒" in catalog

        page.goto(f"{workflow_base_url}/workflow/catalog", wait_until="networkidle")
        page.wait_for_function(
            "() => { const t = document.getElementById('status')?.textContent || ''; "
            "return t && !t.includes('正在读取状态'); }"
        )
        assert "正在读取状态" not in page.locator("#status").inner_text()
        browser.close()
    finally:
        pw_cm.stop()

    assert not console_errors, "页面 console error：\n" + "\n".join(console_errors)
