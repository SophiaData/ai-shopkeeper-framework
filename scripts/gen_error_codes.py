"""生成 docs/error-codes.md（C3：错误码"代码与文档同源"的执行手段）。

跑法：`python scripts/gen_error_codes.py`
CI 会跑 `--check`：文档与登记表不一致就红——因为手抄的文档必然过期。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from shopkeeper.security.error_codes import render_markdown, validate_registry  # noqa: E402

OUT = ROOT / "docs" / "error-codes.md"


def main() -> int:
    problems = validate_registry()
    if problems:
        print("REGISTRY INVARIANTS VIOLATED:")
        for p in problems:
            print("  -", p)
        return 1
    want = render_markdown()
    if "--check" in sys.argv:
        if not OUT.exists():
            print(f"MISSING DOC: {OUT}")
            return 1
        have = OUT.read_text(encoding="utf-8")
        if have.rstrip("\n") != want.rstrip("\n"):
            print("DOC DRIFT: docs/error-codes.md 与登记表不一致，跑 `python scripts/gen_error_codes.py`")
            return 1
        print(f"error-codes doc in sync ({len(want.splitlines())} lines)")
        return 0
    OUT.write_text(want, encoding="utf-8")
    print(f"wrote {OUT} ({len(want.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
