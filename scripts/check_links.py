#!/usr/bin/env python3
"""Markdown 相对链接质量门（零依赖，stdlib）。

校验仓库内所有 Markdown 文件的**相对**链接指向真实存在的路径，作为 ci.ps1 的
一步；本仓库此前没有任何文档链接检查，`docs/review/spec-axis.md` 的 m-3 与
`docs/m1-demo-run.md:157` 两处断链均只被人工评审发现、未被任何门拦住。

设计边界（刻意不做的事，避免假绿）：
- 不校验锚点（`#fragment`）。GitHub 的锚点规则与本地推断不一致（中文标点标题、
  粗体标题的实际 slug 与简化实现有出入），做一半等于给出门一个不可信的通告。
- 不校验引用式定义 `[a]: path` 与裸 HTML `<a href>`。本仓库当前 0 处使用，
  需要时再补，不预先实现没有使用者的分支。

豁免机制：`EXEMPT` 按 (文档路径, 链接目标) 精确匹配。仅用于目标不在版本控制内的
情形，不做前缀通配——豁免越宽越容易掩盖真实断链。

用法：
    python scripts/check_links.py        # 失败退出码 1

失败时输出格式：`<文档>:<行号> -> <目标> (missing)`
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 这些目录不参与扫描：版本控制元数据、依赖、缓存，以及 .gitignore 排除的运行产物。
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "var",
    "artifacts",
    "build",
    "dist",
    # 交接/排障用的本地备份会整份复制 docs，链接相对路径在备份内不成立；
    # 该目录被 .gitignore 排除，不随仓库分发，也不属于文档质量门的对象。
    "backups",
    ".agents",
}

INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^()\s]+)\s*\)")
FENCE = re.compile(r"^\s*(```|~~~)")
FRONT_MATTER = re.compile(r"^---\s*$")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# 行内代码跨行不成立（CommonMark 规定行内 code span 不跨空行），故逐行处理，
# 用反引号串配对剥离。成对的反引号串之外才是正文。
INLINE_CODE = re.compile(r"(`+)(?:[^`]|(?!\1)`)*?\1")

# (文档相对路径, 原始链接目标) —— 目标文件由 .gitignore 刻意排除、不随仓库分发。
EXEMPT: set[tuple[str, str]] = {
    # .gitignore 含 `spike/**/out/`：这些是本地运行产物，证据以文字结论形式
    # 留在 docs/spike/image.md，文件本身不入库。改路径无法修复，故豁免。
    ("docs/spike/image.md", "../../spike/image/out/probe_result.json"),
    ("docs/spike/image.md", "../../spike/image/out/font_glyph_report.txt"),
}


def iter_markdown_files() -> list[Path]:
    found: list[Path] = []
    # 用 os.walk 剪枝：rglob 会先走进 node_modules/target 再过滤，
    # 在大 artifacts/backups 或 Tauri target 目录下会慢到让门不可用。
    for root, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in names:
            if name.endswith(".md"):
                found.append(Path(root) / name)
    return sorted(found)


def strip_non_prose(text: str) -> list[tuple[int, str]]:
    """返回 [(行号, 该行参与链接匹配的文本)]，剔除围栏代码块、front-matter 与行内代码。

    代码块与行内 code span 里的链接是**被引用的文本**，不是真实链接：
    评审报告会引用一个坏链作为反例（如 docs/review/spec-axis.md 的 m-3 条目），
    那属于证据，不应被门当成新的坏链。
    """
    out: list[tuple[int, str]] = []
    in_fence = False
    in_front_matter = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if lineno == 1 and FRONT_MATTER.match(line):
            in_front_matter = True
            continue
        if in_front_matter:
            if FRONT_MATTER.match(line):
                in_front_matter = False
            continue
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((lineno, INLINE_CODE.sub("", line)))
    return out


def is_external(target: str) -> bool:
    """外部链接（http/https/mailto/协议相对）与纯锚点不属于本门范围。"""
    return bool(SCHEME.match(target)) or target.startswith("//") or target.startswith("#")


def check_file(md_path: Path) -> list[str]:
    failures: list[str] = []
    rel_doc = md_path.relative_to(REPO_ROOT).as_posix()
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{rel_doc} -> (无法读取: {exc})"]

    for lineno, line in strip_non_prose(text):
        for match in INLINE_LINK.finditer(line):
            raw = match.group(1).strip()
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1].strip()
            if not raw or is_external(raw):
                continue
            if (rel_doc, raw) in EXEMPT:
                continue
            # 拆出路径部分；fragment 在本版不校验（见模块 docstring）。
            target_path = raw.split("#", 1)[0].split("?", 1)[0]
            if not target_path:
                continue
            resolved = (md_path.parent / target_path).resolve()
            # 用 exists() 而非 is_file()：目录链接（如 [ddl-verify/](./ddl-verify/)）合法。
            if not resolved.exists():
                failures.append(f"{rel_doc}:{lineno} -> {raw} (missing)")
    return failures


def main() -> int:
    files = iter_markdown_files()
    failures: list[str] = []
    for md_path in files:
        failures.extend(check_file(md_path))

    if failures:
        print(f"markdown link gate: {len(failures)} broken link(s) in {len(files)} file(s)")
        for item in failures:
            print(f"  {item}")
        return 1

    print(f"markdown link gate: {len(files)} file(s) checked, all relative links resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
