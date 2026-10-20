"""Tauri 桌面壳脚手架：配置存在 + 轻量检查（不编译 Rust）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_desktop_scaffold.py"


def test_desktop_scaffold_check_passes():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "desktop scaffold OK" in completed.stdout
    assert "does not compile Rust" in completed.stdout


def test_required_paths_exist():
    assert (ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json").is_file()
    assert (ROOT / "apps" / "desktop" / "README.md").is_file()
