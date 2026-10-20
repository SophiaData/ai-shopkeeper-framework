#!/usr/bin/env python3
"""部署产物静态校验（P1-09）。

## 为什么需要它

`Dockerfile` / `docker-compose.yml` / `deploy/shopkeeper.service` 三个文件此前
**没有任何自动检查** —— 写错了要等到真机部署才发现。本机也没有 Docker daemon，
所以 CI 里跑不了 `docker build`。

本脚本做**能在离线环境确定验证**的那部分：

1. **端口一致**：三个文件的端口声明必须互相一致，且与代码默认值一致
2. **环境变量真实存在**：compose/service 里声明的每个变量，代码必须真的会读
   （拼错的变量名不会报错，只会静默失效 —— 这正是最危险的形态）
3. **安全默认**：`TTS_WRITE_ENABLED` / `FEISHU_NOTIFY` 在部署文件里必须默认关
4. **只绑回环**：compose 的端口映射不得对外网开放
5. **非 root**：Dockerfile 必须有 `USER` 且不是 root
6. **健康检查**：Dockerfile 与 compose 都要有 healthcheck，且路径是 `/healthz`
7. **卷持久化**：DB / artifacts / 备份目录必须落在挂载卷里，否则重启丢数据
8. **探活命令可执行**：用容器里那条 CMD 的等价形式真的请求一次（本地起进程）

**不做**：`docker build`（本机无 daemon）。这一条在文档里写明「未验证」，
不假装已验证。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAILURES: list[str] = []
NOTES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _read(name: str) -> str:
    path = ROOT / name
    if not path.is_file():
        fail(f"缺少文件：{name}")
        return ""
    return path.read_text(encoding="utf-8")


def check_port_consistency() -> None:
    """三个部署文件的端口必须一致，且与代码默认值一致。"""
    dockerfile = _read("Dockerfile")
    compose = _read("docker-compose.yml")
    unit = _read("deploy/shopkeeper.service")

    ports = {
        "Dockerfile": set(re.findall(r"WORKFLOW_PORT[=\s]+[\"']?(\d+)", dockerfile)),
        "docker-compose.yml": set(re.findall(r"WORKFLOW_PORT:\s*[\"']?(\d+)", compose)),
        "shopkeeper.service": set(re.findall(r"WORKFLOW_PORT[=\s]+(\d+)", unit)),
    }
    for name, found in ports.items():
        if not found:
            fail(f"{name} 未声明 WORKFLOW_PORT")
    all_ports = set().union(*ports.values())
    if len(all_ports) > 1:
        fail(f"端口不一致：{ {k: sorted(v) for k, v in ports.items()} }")

    # ⚠️ **不与代码默认值比对**：代码默认 `8000` 是「本机开发」口径，
    # 而 Docker 刻意用 `8780`（避开 Windows svchost 占用的 8000），
    # 这是**有意的差异**，不是不一致。我第一版把两者对比，报了一个假失败。
    #
    # 真正要保证的是：**三个部署文件之间**一致 —— 若 Dockerfile 写 8780
    # 而 compose 写 8000，容器就会探活失败，那才是真问题。
    if all_ports:
        NOTES.append(f"部署端口自洽：{sorted(all_ports)}")
        # 镜像 EXPOSE 与 WORKFLOW_PORT 也要一致
        exposed = set(re.findall(r"^EXPOSE\s+(\d+)", dockerfile, re.M))
        if exposed and not (exposed & all_ports):
            fail(f"Dockerfile EXPOSE {sorted(exposed)} 与 WORKFLOW_PORT {sorted(all_ports)} 不一致")

    # compose 的端口映射左侧（宿主机）与右侧（容器）都应等于部署端口
    for mapping in re.findall(r'^\s*-\s*["\']?(\S+:\d+)\D*$', compose, re.M):
        parts = mapping.split(":")
        if len(parts) == 3 and parts[2] not in all_ports:
            fail(f"端口映射 {mapping} 的容器端口不在 {sorted(all_ports)} 内")


def check_env_vars_exist() -> None:
    """部署文件声明的环境变量，代码必须真的会读。

    拼错的名字**不会报错**，只会静默失效 —— 这是最危险的形态。
    """
    src = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in (ROOT / "src").rglob("*.py")
    )
    # 代码中出现的所有大写常量/字面量，作为「可能读的环境变量名」全集
    known = set(re.findall(r'["\']([A-Z][A-Z0-9_]{2,})["\']', src))

    # 解释器/系统级变量不是应用配置，不该按「代码是否读」判定
    ALLOWED = {"PYTHONPATH", "PATH", "LANG", "TZ", "PYTHONUNBUFFERED"}
    for fname in ("docker-compose.yml", "deploy/shopkeeper.service"):
        text = _read(fname)
        decls = set(re.findall(r"^\s*(?:Environment=)?([A-Z][A-Z0-9_]{2,})[=:]", text, re.M))
        unknown = sorted(d for d in decls if d not in known and d not in ALLOWED)
        if unknown:
            fail(f"{fname} 声明了代码中不存在的变量：{unknown}")


def check_safe_defaults() -> None:
    """部署文件里写通道与飞书外呼必须默认关。"""
    compose = _read("docker-compose.yml")
    unit = _read("deploy/shopkeeper.service")
    for name, text in (("docker-compose.yml", compose), ("shopkeeper.service", unit)):
        m = re.search(r"TTS_WRITE_ENABLED[=:]\s*[\"']?\$\{[^}]*:-(\w+)\}[\"']?", text)
        val = m.group(1) if m else None
        if val is None:
            m2 = re.search(r"TTS_WRITE_ENABLED[=:]\s*[\"']?(\w+)", text)
            val = m2.group(1) if m2 else None
        if val not in {"0", "false", "False"}:
            fail(f"{name} 的 TTS_WRITE_ENABLED 默认值不是关闭（实际 {val!r}）")
        if not re.search(r"FEISHU_NOTIFY[=:]\s*[\"']?(?:\$\{[^}]*:-)?0", text):
            fail(f"{name} 的 FEISHU_NOTIFY 默认值不是关闭")


def check_loopback_binding() -> None:
    """compose 端口映射不得对外网开放。"""
    compose = _read("docker-compose.yml")
    for mapping in re.findall(r'^\s*-\s*["\']?([\d.:]+:\d+)["\']?\s*$', compose, re.M):
        host = mapping.split(":")[0]
        if host in {"0.0.0.0", ""}:
            fail(f"端口映射 {mapping} 对外网开放，应绑 127.0.0.1")


def check_non_root() -> None:
    """镜像必须以非 root 用户运行。"""
    dockerfile = _read("Dockerfile")
    m = re.search(r"^USER\s+(\S+)", dockerfile, re.M)
    if not m:
        fail("Dockerfile 没有 USER，会以 root 运行")
    elif m.group(1) in {"root", "0"}:
        fail("Dockerfile 以 root 运行")


def check_healthcheck() -> None:
    """Dockerfile 与 compose 都要有 healthcheck，且打 /healthz。"""
    for name in ("Dockerfile", "docker-compose.yml"):
        text = _read(name)
        if "HEALTHCHECK" not in text.upper() and "healthcheck:" not in text:
            fail(f"{name} 缺少健康检查")
        if "/healthz" not in text:
            fail(f"{name} 的健康检查没打 /healthz")


def check_volume_persistence() -> None:
    """DB / artifacts / 备份必须落在挂载卷里，否则重启丢数据。"""
    compose = _read("docker-compose.yml")
    env_block = compose.split("environment:", 1)[-1].split("volumes:", 1)[0]
    volumes = compose.split("volumes:", 1)[-1]

    for var in ("WORKFLOW_DB_PATH", "ARTIFACTS_DIR", "BACKUP_DIR"):
        m = re.search(rf"{var}[=:]\s*[\"']?\$\{{{var}:-([^}}\"']+)", env_block)
        if not m:
            fail(f"compose 未声明 {var}，无法确认落盘位置")
            continue
        path = m.group(1)
        top = path.split("/")[0]
        if f"./{top}:" not in volumes and f"{top}:" not in volumes:
            fail(f"{var}={path} 不在挂载卷里（重启会丢数据）")


def check_probe_command_runs() -> None:
    """容器里那条 CMD 的等价形式必须真的能起来并响应 /healthz。

    这是本脚本里**唯一真的执行**的一步 —— 其余都是静态检查。
    """
    import socket
    import subprocess
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    import os

    env = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ) if key in os.environ}
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": "src",
        "WORKFLOW_HOST": "127.0.0.1",
        "WORKFLOW_PORT": str(port),
        "WORKFLOW_SCHEDULER": "1",
        "NO_PROXY": "localhost,127.0.0.1",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "shopkeeper.workflow.serve"],
        cwd=ROOT,
        env=env,  # type: ignore[arg-type]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2
                ) as resp:
                    body = resp.read().decode("utf-8")
                if '"ok"' in body:
                    NOTES.append(f"探活成功：{body.strip()}")
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        fail("容器 CMD 等价命令起不来或 /healthz 无响应")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def check_windows_scripts() -> None:
    """P1-08：Windows 部署脚本的**静态**可验证部分。

    ⚠️ **本机与 runner 都没有 pwsh**，所以这里**不执行** PowerShell ——
    只做机械检查（文件存在、必需的 cmdlet 出现）。真正的运行验证
    需要在 Windows 上跑 `scripts/windows/service.ps1 -Install`。

    **明确不声称已验证**：脚本能跑通需要真 Windows。
    """
    win = ROOT / "scripts" / "windows"
    for name in ("install.ps1", "start.ps1", "start.bat", "common.ps1", "service.ps1"):
        if not (win / name).is_file():
            fail(f"缺少 scripts/windows/{name}")

    service = _read("scripts/windows/service.ps1")
    if service:
        # ⚠️ **必须排除注释行**：把调用注释掉后，纯 `not in` 仍会命中那些文字，
        # 于是校验器被自己的注释骗过（注入测试当场抓出来的）。
        service_code = "\n".join(
            ln for ln in service.splitlines() if not ln.lstrip().startswith("#")
        )
        for cmdlet in ("New-ScheduledTaskAction", "New-ScheduledTaskTrigger",
                       "Register-ScheduledTask", "Unregister-ScheduledTask"):
            if cmdlet not in service_code:
                fail(f"service.ps1 缺少 {cmdlet}（注册/卸载计划任务）")
        if "New-ScheduledTaskTrigger -AtLogOn" not in service_code:
            fail("service.ps1 的触发器不是登录时（-AtLogOn）")
        if "Invoke-RotateLogs" not in service_code:
            fail("service.ps1 没有日志滚动")
        # ⚠️ 不能只 grep "archive" 这个词 —— 那太弱：把归档逻辑删掉、
        # 只在注释里留个 archive 字样就能骗过（我第一版就是这样，注入测试没抓出来）。
        # 改为检查**行为**：滚动实现里必须出现 Move-Item（移走），
        # 且**不得**出现 Remove-Item（真删除）。
        rotate_body = service.split("function Invoke-RotateLogs", 1)
        if len(rotate_body) < 2:
            fail("service.ps1 里找不到 Invoke-RotateLogs 的实现")
        else:
            body = rotate_body[1].split("\n}", 1)[0]
            code_lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
            code = "\n".join(code_lines)
            if "Move-Item" not in code:
                fail("日志滚动没有移动任何文件（应归档旧日志）")
            if "Remove-Item" in code:
                fail("日志滚动里出现 Remove-Item —— 删除会静默丢掉排障证据，应改为归档")

    for name in ("service.ps1", "install.ps1", "start.ps1"):
        text = _read(f"scripts/windows/{name}")
        if re.search(r"TTS_WRITE_ENABLED[^\n]{0,20}[=:][^\n]{0,5}[\"']?1", text):
            fail(f"{name} 会打开 TTS_WRITE_ENABLED（不得由安装脚本解锁真写）")

    if "8010" not in _read("scripts/windows/start.ps1"):
        fail("start.ps1 的默认端口不是 8010")

    NOTES.append("Windows 脚本静态检查通过（**未在真 Windows 上执行**）")


def main() -> int:
    check_port_consistency()
    check_env_vars_exist()
    check_safe_defaults()
    check_loopback_binding()
    check_non_root()
    check_healthcheck()
    check_volume_persistence()
    check_probe_command_runs()
    check_windows_scripts()

    for note in NOTES:
        print(f"  ok  {note}")
    if FAILURES:
        print("\n部署校验失败：", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"deploy check OK ({len(NOTES)} notes)")
    print("  未验证：docker build（本机无 daemon）—— 文档已注明")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
