"""把选品报告、三语文案和已落盘的商品图收进同一个交付目录。

以前 `packages/` 里只有 listing JSON。`output_path` 指向包外的
`artifacts/demo/images/`，MockImage 又只返回路径、不写字节，打开交付目录时图是 0。

本模块只复制已经存在且非空的本地文件，并把 JSON 里的 `output_path` 改成包内相对路径。
`original_kept` 和 http(s) 源图引用不是本地产物，不复制，也不另造一张图来填数。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


def materialize_delivery_package(
    listing: dict[str, Any],
    *,
    report: dict[str, Any] | None,
    packages_dir: Path,
    wall_s: float | None = None,
    wall_scope: str = "端到端：选品开始到交付包写完",
    source_url: str | None = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    listing_id = str(listing.get("listing_id") or "x")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", listing_id)[:80] or "x"
    pkg_dir = packages_dir / f"listing_{safe}"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)

    packaged = json.loads(json.dumps(listing))
    image_files: list[Path] = []
    missing: list[str] = []
    images_by_lang = packaged.get("images_by_lang") or {}
    rewritten: dict[str, list[dict[str, Any]]] = {}
    for lang, assets in images_by_lang.items():
        lang_assets: list[dict[str, Any]] = []
        for index, asset in enumerate(assets or [], start=1):
            item = dict(asset)
            copied, reason = _copy_image(item, pkg_dir, str(lang), index)
            if copied is not None:
                item["output_path"] = copied.relative_to(pkg_dir).as_posix()
                item["package_status"] = "copied"
                image_files.append(copied)
            else:
                item["package_status"] = reason
                missing.append(f"{lang}: {reason} ({item.get('output_path')})")
            lang_assets.append(item)
        rewritten[str(lang)] = lang_assets
    packaged["images_by_lang"] = rewritten
    packaged["delivery"] = {
        "source_url": source_url,
        "product_id": product_id,
        "wall_s": None if wall_s is None else round(wall_s, 3),
        "wall_scope": wall_scope,
        "publish": False,
        "note": "纯生成。上架、改价、发货、退款在 TikTok Shop 卖家中心由你本人操作。",
    }

    copy_files = _write_copy(pkg_dir, packaged.get("versions") or [])
    report_path = pkg_dir / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "source_url": source_url,
                "product_id": product_id,
                "report": report or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    listing_path = pkg_dir / "listing.json"
    listing_path.write_text(json.dumps(packaged, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = pkg_dir / "index.md"
    index_path.write_text(
        _index(
            source_url=source_url,
            product_id=product_id,
            wall_s=wall_s,
            wall_scope=wall_scope,
            copy_files=copy_files,
            image_files=image_files,
            missing=missing,
            pkg_dir=pkg_dir,
        ),
        encoding="utf-8",
    )

    langs = list(packaged.get("langs") or [])
    images_complete = bool(langs) and all(
        any(a.get("package_status") == "copied" for a in rewritten.get(lang, []))
        for lang in langs
    )
    files = [report_path, listing_path, index_path, *copy_files, *image_files]
    return {
        "directory": pkg_dir,
        "files": [str(p) for p in files],
        "image_files": [str(p) for p in image_files],
        "missing_images": missing,
        "images_complete": images_complete,
        "listing": packaged,
    }


def _copy_image(asset: dict[str, Any], pkg_dir: Path, lang: str, index: int) -> tuple[Path | None, str]:
    if asset.get("mode") == "original_kept":
        return None, "not_a_file"
    raw = asset.get("output_path")
    if not raw:
        return None, "not_a_file"
    text = str(raw)
    if text.startswith(("http://", "https://")):
        return None, "not_a_file"
    src = Path(text)
    if not src.is_file():
        return None, "missing"
    if src.stat().st_size <= 0:
        return None, "empty"
    suffix = src.suffix if src.suffix else ".png"
    target = pkg_dir / "images" / lang / f"{index:02d}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    if target.stat().st_size <= 0:
        target.unlink(missing_ok=True)
        return None, "empty"
    return target, "copied"


def _write_copy(pkg_dir: Path, versions: list[dict[str, Any]]) -> list[Path]:
    written: list[Path] = []
    copy_dir = pkg_dir / "copy"
    copy_dir.mkdir(parents=True, exist_ok=True)
    for version in versions:
        lang = str(version.get("lang") or "").upper()
        if not lang:
            continue
        lines = [str(version.get("title") or "").strip(), ""]
        for bullet in version.get("bullets") or []:
            text = str(bullet).strip()
            if text:
                lines.append(f"- {text}")
        lines.append("")
        lines.append(str(version.get("description") or "").strip())
        body = "\n".join(lines).strip()
        if not body:
            continue
        path = copy_dir / f"{lang}.txt"
        path.write_text(body + "\n", encoding="utf-8")
        written.append(path)
    return written


def _index(
    *,
    source_url: str | None,
    product_id: str | None,
    wall_s: float | None,
    wall_scope: str,
    copy_files: list[Path],
    image_files: list[Path],
    missing: list[str],
    pkg_dir: Path,
) -> str:
    clock = "未记录" if wall_s is None else f"{wall_s:.1f}s"
    lines = [
        "# M1 Demo 交付包",
        "",
        f"- 链接：`{source_url or '—'}`",
        f"- 商品：`{product_id or '—'}`",
        f"- 墙钟：{clock}",
        f"- 计时口径：{wall_scope}。门槛 180 秒。",
        "- 上架：本目录是纯生成。上架、改价、发货、退款在 TikTok Shop 卖家中心由你本人操作。",
        "",
        "## 选品报告",
        "",
        "- `report.json`",
        "",
        "## 文案",
        "",
    ]
    if copy_files:
        for path in copy_files:
            lines.append(f"- `{path.relative_to(pkg_dir).as_posix()}`")
    else:
        lines.append("- （没有非空文案）")
    lines += ["", "## 商品图", ""]
    if image_files:
        for path in image_files:
            lines.append(f"- `{path.relative_to(pkg_dir).as_posix()}`")
    else:
        lines.append("- （没有复制进包的图片文件）")
    if missing:
        lines += ["", "## 未进包的图", ""]
        lines.extend(f"- {item}" for item in missing)
    lines.append("")
    return "\n".join(lines)
