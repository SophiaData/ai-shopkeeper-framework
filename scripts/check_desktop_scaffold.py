#!/usr/bin/env python3
"""轻量校验 apps/desktop Tauri 脚手架（不编译 Rust、不 npm install）。

完成条件：配置文件存在且可解析；壳指向本机 /workflow；无明文密钥形态。
供本地与 CI（含 Windows pytest）使用。
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "apps" / "desktop"
SRC_TAURI = DESKTOP / "src-tauri"

REQUIRED_FILES = [
    DESKTOP / "README.md",
    DESKTOP / "package.json",
    DESKTOP / "ui" / "index.html",
    SRC_TAURI / "Cargo.toml",
    SRC_TAURI / "tauri.conf.json",
    SRC_TAURI / "build.rs",
    SRC_TAURI / "src" / "main.rs",
    SRC_TAURI / "src" / "lib.rs",
    SRC_TAURI / "capabilities" / "default.json",
    SRC_TAURI / "icons" / "32x32.png",
    SRC_TAURI / "icons" / "128x128.png",
    SRC_TAURI / "icons" / "icon.ico",
]

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"LTAI[0-9A-Za-z]{12,}"),
    re.compile(r"AccessKey\s*Secret\s*[:=]\s*\S", re.I),
]

# Tauri 2 的 core 权限白名单。capabilities 里写错名字时 `cargo check` 会失败，
# 但那要装 Rust 才跑得到；本脚本不编译 Rust，所以在这里挡住。
#
# 2026-10-14 修：原先 default.json 声明了 `core:webview:allow-navigate`，Tauri 2
# 没有这个权限（navigate 是 Rust 侧 `WebviewWindow::navigate`，不经 IPC，本就不需要
# 授权），导致 `cargo check` 直接失败，而本脚本只检查文件存在、照样放行。
# 来源：tauri 2.12.3 的 `Permission core:webview:allow-navigate not found` 报错列出
# 的全部合法 core:webview 权限，以及 core:default 聚合集。
CORE_PERMISSION_PREFIXES = ("core:",)

# core:webview 下真实存在的权限（据 tauri 2.12.3 报错清单）。新增用到的权限时同步。
KNOWN_CORE_WEBVIEW_PERMISSIONS = {
    "core:webview:default",
    "core:webview:allow-clear-all-browsing-data",
    "core:webview:allow-create-webview",
    "core:webview:allow-create-webview-window",
    "core:webview:allow-get-all-webviews",
    "core:webview:allow-internal-toggle-devtools",
    "core:webview:allow-print",
    "core:webview:allow-reparent",
    "core:webview:allow-set-webview-auto-resize",
    "core:webview:allow-set-webview-background-color",
    "core:webview:allow-set-webview-focus",
    "core:webview:allow-set-webview-position",
    "core:webview:allow-set-webview-size",
    "core:webview:allow-set-webview-zoom",
    "core:webview:allow-webview-close",
    "core:webview:allow-webview-hide",
    "core:webview:allow-webview-position",
    "core:webview:allow-webview-show",
    "core:webview:allow-webview-size",
    "core:webview:deny-clear-all-browsing-data",
    "core:webview:deny-create-webview",
    "core:webview:deny-create-webview-window",
    "core:webview:deny-get-all-webviews",
    "core:webview:deny-internal-toggle-devtools",
    "core:webview:deny-print",
    "core:webview:deny-reparent",
    "core:webview:deny-set-webview-auto-resize",
    "core:webview:deny-set-webview-background-color",
    "core:webview:deny-set-webview-focus",
    "core:webview:deny-set-webview-position",
    "core:webview:deny-set-webview-size",
    "core:webview:deny-set-webview-zoom",
    "core:webview:deny-webview-close",
    "core:webview:deny-webview-hide",
    "core:webview:deny-webview-position",
    "core:webview:deny-webview-show",
    "core:webview:deny-webview-size",
}

# core 下其他已知的聚合权限（非 webview 段），按需扩充。
KNOWN_CORE_AGGREGATES = {
    "core:default",
    "core:app:default",
    "core:event:default",
    "core:image:default",
    "core:menu:default",
    "core:path:default",
    "core:resources:default",
    "core:tray:default",
    "core:window:default",
}


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    for path in REQUIRED_FILES:
        if not path.is_file():
            _fail(f"missing required file: {path.relative_to(ROOT)}")
        if path.stat().st_size <= 0:
            _fail(f"empty file: {path.relative_to(ROOT)}")

    conf_path = SRC_TAURI / "tauri.conf.json"
    conf = json.loads(conf_path.read_text(encoding="utf-8"))
    build = conf.get("build") or {}
    dev_url = str(build.get("devUrl") or "")
    if "/workflow" not in dev_url:
        _fail(f"tauri.conf.json build.devUrl must point at /workflow, got {dev_url!r}")
    if "127.0.0.1" not in dev_url and "localhost" not in dev_url:
        _fail(f"tauri.conf.json build.devUrl must be loopback, got {dev_url!r}")
    frontend_dist = str(build.get("frontendDist") or "")
    if "ui" not in frontend_dist.replace("\\", "/"):
        _fail(f"frontendDist should be the local ui/ fallback, got {frontend_dist!r}")
    windows = (conf.get("app") or {}).get("windows") or []
    if not any(w.get("label") == "main" for w in windows if isinstance(w, dict)):
        _fail("tauri.conf.json app.windows must include label=main")

    cargo = tomllib.loads((SRC_TAURI / "Cargo.toml").read_text(encoding="utf-8"))
    pkg = cargo.get("package") or {}
    if pkg.get("name") != "shopkeeper-desktop":
        _fail(f"Cargo.toml package.name must be shopkeeper-desktop, got {pkg.get('name')!r}")
    deps = cargo.get("dependencies") or {}
    if "tauri" not in deps:
        _fail("Cargo.toml must depend on tauri")

    lib_rs = (SRC_TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
    if "WORKFLOW_PORT" not in lib_rs:
        _fail("src/lib.rs must read WORKFLOW_PORT")
    if "/workflow" not in lib_rs:
        _fail("src/lib.rs must open /workflow")
    if "127.0.0.1" not in lib_rs:
        _fail("src/lib.rs must use 127.0.0.1 loopback")

    ui = (DESKTOP / "ui" / "index.html").read_text(encoding="utf-8")
    if "/workflow" not in ui:
        _fail("ui/index.html must redirect to /workflow")

    pkg_json = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    if "@tauri-apps/cli" not in (pkg_json.get("devDependencies") or {}):
        _fail("package.json must list @tauri-apps/cli")

    # capabilities：权限名必须真实存在。写错时 `cargo check` 才报错，而本脚本不编译
    # Rust——所以在这里按白名单挡住，避免"静态检查绿、真编译红"。
    cap_path = SRC_TAURI / "capabilities" / "default.json"
    cap = json.loads(cap_path.read_text(encoding="utf-8"))
    cap_windows = cap.get("windows")
    if not isinstance(cap_windows, list) or "main" not in cap_windows:
        _fail("capabilities/default.json must target the main window")
    permissions = cap.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        _fail("capabilities/default.json must declare at least one permission")

    def _permission_name(entry: object) -> str:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            return str(entry.get("identifier") or "")
        return ""

    known = KNOWN_CORE_WEBVIEW_PERMISSIONS | KNOWN_CORE_AGGREGATES
    for entry in permissions:
        name = _permission_name(entry)
        if not name:
            _fail(f"capabilities/default.json has a permission without a name: {entry!r}")
        if name.startswith(CORE_PERMISSION_PREFIXES) and name not in known:
            _fail(
                f"capabilities/default.json declares unknown Tauri core permission "
                f"{name!r}; cargo check will fail. Known core:webview permissions: "
                f"{len(KNOWN_CORE_WEBVIEW_PERMISSIONS)} entries; if this is a real "
                f"permission from a newer Tauri, add it to KNOWN_CORE_* in this script."
            )

    readme = (DESKTOP / "README.md").read_text(encoding="utf-8")
    for needle in ("WORKFLOW_PORT", "shopkeeper.workflow.serve", "npm run dev", "Windows"):
        if needle not in readme:
            _fail(f"apps/desktop/README.md missing Windows/start guidance: {needle!r}")

    # no secrets in scaffold text files
    text_files = [
        conf_path,
        SRC_TAURI / "Cargo.toml",
        SRC_TAURI / "src" / "lib.rs",
        SRC_TAURI / "src" / "main.rs",
        DESKTOP / "package.json",
        DESKTOP / "README.md",
        DESKTOP / "ui" / "index.html",
    ]
    for path in text_files:
        body = path.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            if pat.search(body):
                _fail(f"secret-like pattern in {path.relative_to(ROOT)}")

    print("desktop scaffold OK")
    print(f"  root: {DESKTOP.relative_to(ROOT)}")
    print(f"  devUrl: {dev_url}")
    print("  note: this check does not compile Rust or run tauri build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
