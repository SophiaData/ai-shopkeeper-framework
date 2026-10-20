"""工作台内嵌 JS：防 Python 字符串把 \\n 解成换行；node --check 语法门。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.routes import PAGE, mount_workflow
from shopkeeper.workflow.service import WorkflowService
from tests.workflow.env_isolation import isolate_runtime_env

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _client(tmp_path, monkeypatch) -> TestClient:
    isolate_runtime_env(monkeypatch, tmp_path / "rt", platform="mock")
    app = FastAPI()
    mount_workflow(
        app,
        WorkflowService.from_env(
            {
                "WORKFLOW_DB_PATH": str(tmp_path / "rt" / "workflow.db"),
                "WORKFLOW_LLM_PROVIDER": "mock",
                "WORKFLOW_PLATFORM": "mock",
            }
        ),
    )
    return TestClient(app)


def test_page_template_keeps_js_newline_escape():
    """PAGE 必须是 raw / 双反斜杠，运行时源码里仍是 join(\"\\n\") 两字符。"""
    assert 'lines.join("\\n")' in PAGE
    assert 'lines.join("\n")' not in PAGE


def test_workflow_and_catalog_scripts_pass_node_check(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 JS 语法门")

    client = _client(tmp_path, monkeypatch)
    for path in ("/workflow", "/workflow/catalog", "/workflow/profit"):
        html = client.get(path).text
        assert 'lines.join("\\n")' in html
        assert "正在读取状态" in html
        scripts = _SCRIPT_RE.findall(html)
        assert scripts, f"{path} 没有 <script>"
        for index, script in enumerate(scripts):
            js_path = tmp_path / f"{path.strip('/').replace('/', '_')}_{index}.js"
            js_path.write_text(script, encoding="utf-8")
            completed = subprocess.run(
                [node, "--check", str(js_path)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert completed.returncode == 0, (
                f"{path} script[{index}] node --check 失败：\n{completed.stderr}\n---\n{script[:400]}"
            )
