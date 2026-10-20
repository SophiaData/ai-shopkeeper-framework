"""真 API 套件静态护栏：不依赖外网；默认 CI（`-m "not real"`）也会跑。

保证 `tests/real/` 源码不出现写接口调用名，也不把 `TTS_WRITE_ENABLED` 设为放行。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.real.env_helpers import WRITE_METHOD_NAMES

_REAL_DIR = Path(__file__).resolve().parent
# 仅允许「断言写闸门仍拦截」的探测调用（不外呼；见 test_tiktok_write_paths_still_blocked）。
_ALLOWED_WRITE_PROBES = frozenset(
    {
        ("test_tiktok_readonly.py", "create_listing"),
    }
)
_FORBIDDEN_ENV_ASSIGN = {
    ("TTS_WRITE_ENABLED", "1"),
    ("TTS_WRITE_ENABLED", "true"),
    ("TTS_WRITE_ENABLED", "True"),
    ("TTS_WRITE_ENABLED", "yes"),
    ("TTS_WRITE_ENABLED", "on"),
}


def test_real_suite_source_has_no_write_api_calls():
    """扫描 tests/real/*.py：禁止调用上架/发货/退款/下单/付款等写路径。"""
    forbidden = set(WRITE_METHOD_NAMES)
    offenders: list[str] = []
    for path in sorted(_REAL_DIR.glob("*.py")):
        if path.name == "test_suite_guards.py":
            # 本文件会在字面量里提到禁止名单，跳过自身。
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name in forbidden and (path.name, name) not in _ALLOWED_WRITE_PROBES:
                    offenders.append(f"{path.name}:{node.lineno}:{name}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                key = _env_assign_key(target)
                if key and (key, node.value.value) in _FORBIDDEN_ENV_ASSIGN:
                    offenders.append(f"{path.name}:{node.lineno}:set {key}={node.value.value}")
    assert not offenders, "tests/real 不得调用写接口或放行 TTS_WRITE: " + "; ".join(offenders)


def test_real_suite_constructs_tiktok_without_allow_writes():
    """禁止 TikTokShopClient(..., allow_writes=True)。"""
    offenders: list[str] = []
    for path in sorted(_REAL_DIR.glob("test_*.py")):
        if path.name == "test_suite_guards.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node.func) != "TikTokShopClient":
                continue
            for kw in node.keywords:
                if kw.arg == "allow_writes" and _is_true_literal(kw.value):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "TikTokShopClient 不得 allow_writes=True: " + ", ".join(offenders)


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _env_assign_key(target: ast.AST) -> str | None:
    # os.environ["TTS_WRITE_ENABLED"] = ...
    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
        if isinstance(target.slice.value, str):
            return target.slice.value
    return None


def _is_true_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True
