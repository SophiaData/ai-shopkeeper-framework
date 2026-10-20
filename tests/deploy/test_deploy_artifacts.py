"""部署产物校验（P1-09）。

`Dockerfile` / `docker-compose.yml` / `deploy/shopkeeper.service` 此前
**没有任何自动检查** —— 写错了要等真机部署才发现。本机也没有 Docker daemon，
所以 CI 里跑不了 `docker build`。

`scripts/check_deploy.py` 做**能在离线环境确定验证**的那部分。
本文件把它的每条规则**独立钉一遍**：直接构造坏输入，断言校验器会报出来。

**为什么不只跑一次 `check_deploy.py` 就算**：
那样只证明「当前这三个文件没被改坏」，不能证明**校验器本身有效**。
下面每条用例都注入一种历史上的真实失误形态。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_deploy.py"


def _run() -> tuple[int, str]:
    child_env = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ) if key in os.environ}
    child_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_PROXY": "localhost,127.0.0.1"})
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_validator_passes_on_current_files():
    """当前仓库的三个部署文件必须通过。"""
    code, out = _run()
    assert code == 0, f"校验器报错：\n{out}"
    assert "deploy check OK" in out


def test_probe_really_started_a_server():
    """**唯一真的执行**的那一步：容器 CMD 的等价命令必须真的起得来。

    这条断言存在的意义：若只做静态检查，`serve` 起不来这种事**永远不会被发现**。
    """
    _code, out = _run()
    assert "探活成功" in out, f"未执行真探活：\n{out}"
    assert '"ok"' in out


@pytest.mark.parametrize(
    "filename,old,new,expect",
    [
        # 端口在三个文件之间不一致 → 容器探活会失败
        ("docker-compose.yml", 'WORKFLOW_PORT: "8780"', 'WORKFLOW_PORT: "8000"', "端口不一致"),
        # 写通道被默认打开（最危险的部署失误）
        (
            "docker-compose.yml",
            'TTS_WRITE_ENABLED: "${TTS_WRITE_ENABLED:-0}"',
            'TTS_WRITE_ENABLED: "${TTS_WRITE_ENABLED:-1}"',
            "TTS_WRITE_ENABLED",
        ),
        # 绑到外网
        (
            "docker-compose.yml",
            '"127.0.0.1:8780:8780"',
            '"0.0.0.0:8780:8780"',
            "对外网开放",
        ),
        # 环境变量拼错 —— 不报错，只是静默失效
        (
            "docker-compose.yml",
            "PII_RETENTION_DAYS:",
            "PII_RETENTION_DAYZ:",
            "不存在的变量",
        ),
        # 以 root 运行
        ("Dockerfile", "USER shopkeeper", "# USER shopkeeper", "root"),
    ],
)
def test_validator_catches_injected_defect(filename, old, new, expect):
    """**每条规则都要有牙**：注入一种真实失误，校验器必须报出来。"""
    path = ROOT / filename
    original = path.read_text(encoding="utf-8")
    assert old in original, f"前置条件不成立：{filename} 里没有 {old!r}"
    try:
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, out = _run()
        assert code != 0, f"注入 {expect} 后校验器仍通过：\n{out}"
        assert expect in out, f"未报出期望的 {expect!r}：\n{out}"
    finally:
        path.write_text(original, encoding="utf-8")
    # 恢复后必须重新变绿
    assert _run()[0] == 0, "恢复后校验器仍失败"


def test_ci_keeps_the_three_protected_job_names():
    """CI 里三个 job 的**显示名**不得改动 —— 分支保护依赖它们。

    本 PR 往已有 job 里**加 step**（而不是加 job），正是为了不动这份名单。
    """
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for required in (
        "name: pytest (not real)",
        "name: pytest (not real, windows)",
        "name: desktop scaffold (no rustc)",
    ):
        assert required in text, f"缺少受保护的 job 名：{required}"
    # ⚠️ 必须**只看 `jobs:` 之后**的部分 —— 否则会把 `on:` 下的
    # `push:` / `pull_request:` 也算成 job（我第一版就是这样误报的）。
    jobs_section = text.split("\njobs:", 1)[1]
    jobs = re.findall(r"^  ([a-z][a-z0-9_-]*):$", jobs_section, re.M)

    # 断言的是**三个受保护 job 仍在**，而不是「总数恰好 3」——
    # P3-01 有意新增了 `desktop-compile`（非必需、continue-on-error），
    # 写死总数会把「有意新增」误判成「改坏了受保护配置」。
    for required in ("pytest", "pytest-windows", "desktop-scaffold"):
        assert required in jobs, f"受保护 job 缺失：{required}（实际 {jobs}）"


def test_deploy_check_wired_into_both_ci_entrypoints():
    """本地 CI 与 GitHub CI 都要跑这个校验，否则等于没加。"""
    gh = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_deploy.py" in gh

    local = (ROOT / "scripts" / "ci.ps1").read_text(encoding="utf-8")
    assert "check_deploy.py" in local


# --------------------------------------------------------------------------
# P1-08：Windows 服务化（静态可验证部分）
# --------------------------------------------------------------------------


def test_windows_service_script_exists():
    """schedule + 日志滚动脚本必须在。"""
    win = ROOT / "scripts" / "windows"
    for name in ("service.ps1", "install.ps1", "start.ps1", "common.ps1", "start.bat"):
        assert (win / name).is_file(), f"缺少 scripts/windows/{name}"


def test_windows_service_has_logon_trigger():
    """开机自启必须是**登录时**触发。

    为什么不是 `-AtStartup`：真服务会在 session 0 跑、没有桌面，
    而本产品要打开浏览器 / 被桌面壳访问 —— 必须在交互会话里。
    """
    text = (ROOT / "scripts" / "windows" / "service.ps1").read_text(encoding="utf-8")
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "Register-ScheduledTask" in text
    assert "Unregister-ScheduledTask" in text, "必须能卸载，否则装了删不掉"


def test_log_rotation_archives_instead_of_deleting():
    """**行为**断言：滚动必须移动文件，不得删除。

    删除会在滚动误配（例如 MaxLogMB 填成 1）时**静默丢掉排障证据**。
    只 grep "archive" 字样太弱 —— 把逻辑删掉只在注释里留个词就能骗过，
    所以直接看实现体里有没有 `Remove-Item`。
    """
    text = (ROOT / "scripts" / "windows" / "service.ps1").read_text(encoding="utf-8")
    body = text.split("function Invoke-RotateLogs", 1)[1].split(chr(10) + "}", 1)[0]
    code = chr(10).join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "Move-Item" in code, "滚动没有移动任何文件"
    assert "Remove-Item" not in code, "滚动里出现了 Remove-Item，应改为归档"


@pytest.mark.parametrize(
    "fname,old,new,expect",
    [
        (
            "scripts/windows/service.ps1",
            "Register-ScheduledTask -TaskName",
            "# Register-ScheduledTask -TaskName",
            "Register-ScheduledTask",
        ),
        (
            "scripts/windows/service.ps1",
            "New-ScheduledTaskTrigger -AtLogOn",
            "New-ScheduledTaskTrigger -AtStartup",
            "AtLogOn",
        ),
        (
            "scripts/windows/install.ps1",
            "TTS_WRITE_ENABLED",
            "TTS_WRITE_ENABLED=1",
            "TTS_WRITE_ENABLED",
        ),
    ],
)
def test_deploy_check_catches_windows_defects(fname, old, new, expect):
    """注入式反向用例：Windows 侧的每条规则都要有牙。"""
    path = ROOT / fname
    original = path.read_text(encoding="utf-8")
    assert old in original, f"前置条件不成立：{fname} 里没有 {old!r}"
    try:
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, out = _run()
        assert code != 0, f"注入后校验器仍通过：\n{out}"
        assert expect in out, f"未报出 {expect!r}：\n{out}"
    finally:
        path.write_text(original, encoding="utf-8")
    assert _run()[0] == 0, "恢复后校验器仍失败"
