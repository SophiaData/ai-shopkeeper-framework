# AI 店长 · Tauri 桌面壳

按 [DESIGN §5](../../docs/DESIGN.md)：本壳**加载与浏览器同一份工作台前端**（FastAPI 内嵌的 `/workflow`），不另写一套 UI，也不依赖 Docker。

默认打开：

```text
http://127.0.0.1:${WORKFLOW_PORT:-8000}/workflow
```

## 前提

| 依赖 | 说明 |
|------|------|
| Python 工作台 | 先起 `python -m shopkeeper.workflow.serve`（见仓库根 README） |
| Rust + Cargo | [rustup](https://rustup.rs/)；Windows 需 MSVC 构建工具 |
| Node.js 20+ | 仅用于 `@tauri-apps/cli` |
| WebView2 | Windows 10/11 通常已自带 |

**不要**把真实密钥写进本目录或提交进 Git。发货 / 退款仍由卖家中心人工处理。

## Windows 启动步骤

在仓库根目录打开两个终端（PowerShell 或 cmd）。

**终端 1 — 先起 Python 工作台：**

```bat
cd /d path\to\ai-shopkeeper
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev]"
set PYTHONPATH=src
set WORKFLOW_PORT=8000
python -m shopkeeper.workflow.serve
```

浏览器应能打开 `http://127.0.0.1:8000/workflow`。若 8000 被占用：

```bat
set WORKFLOW_PORT=8010
python -m shopkeeper.workflow.serve
```

**终端 2 — 再起 Tauri：**

```bat
cd /d path\to\ai-shopkeeper\apps\desktop
npm install
set WORKFLOW_PORT=8000
npm run dev
```

`WORKFLOW_PORT` 须与终端 1 一致。壳启动后会导航到该端口的 `/workflow`。

等价于：

```bat
npx tauri dev
```

## 轻量检查（不编译 Rust）

仓库 CI / 本地可跑（不下载 crate、不跑 `cargo build`）：

```bash
python scripts/check_desktop_scaffold.py
```

或：

```bash
PYTHONPATH=src python -m pytest -m "not real" tests/desktop/test_scaffold.py -q
```

完整 `cargo build` / `tauri build` 为可选本地步骤，默认 CI **不**编译整棵 Rust，以免超时。

## 目录

```text
apps/desktop/
  README.md                 本说明
  package.json              @tauri-apps/cli
  ui/index.html             回退页（跳转到本机 /workflow；非第二套业务 UI）
  src-tauri/
    tauri.conf.json         Tauri 2 配置（devUrl 默认 :8000/workflow）
    Cargo.toml
    src/lib.rs              读 WORKFLOW_PORT 并 navigate
    capabilities/           仅允许 loopback 导航
    icons/                  占位图标
```
