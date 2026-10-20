"""短视频脚本 → 素材包导出（HANDOFF_PLAN P2-06）。

## 这是给拍摄用的，不是发布器

导出产出三样东西，全部落在**本地目录**：分镜 md（照着拍）、分镜 CSV（进表格排期）、
封面图 PNG（Pillow 画出来，直接当封面草稿用）。

**不发布、不调平台、不上传**。本仓库不代发短视频（与 P0-11 的真写验收是两件事）。

## 为什么封面用 Pillow 画而不是调图像模型

封面文字是脚本里已经定好的 `covers[].text_th`，把它画成图是**确定性**的 ——
排版稳定、零成本、离线可跑、可复现。调图像模型既不确定又要钱，还可能与文案不符。
所以这里只用 Pillow 画字，字体缺失时**降级为不画字但仍出图**，而不是抛错。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

#: 分镜 CSV 的列（顺序稳定，便于人工排期）。
SHOT_COLUMNS: tuple[str, ...] = (
    "shot_no",
    "seconds",
    "beat",
    "visual",
    "voice_th",
    "voice_zh",
    "subtitle_th",
    "note",
)

#: 封面默认尺寸（竖版 9:16，TikTok 规格）。
COVER_SIZE = (1080, 1920)

#: 候选字体路径，按平台顺序找。找不到就用 Pillow 内置位图字体（能画，但小）。
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _dict_list(script: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """取一个「应当由 dict 组成的列表」字段，**逐个过滤掉非 dict 元素**。

    只检查容器是 list 不够：`{"covers": ["纯字符串"]}` 会让下面的 `.get()` 抛
    `AttributeError`，而 CLI 层没包这一层，用户看到的是 Python traceback 而不是
    结构化的 `SCRIPT_INPUT_INVALID`。脚本 JSON 由 LLM 或人手生成，
    混进一个字符串是**现实**的输入，不该崩。
    """
    raw = script.get(key)
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _shots(script: dict[str, Any]) -> list[dict[str, Any]]:
    return _dict_list(script, "shots")


def render_storyboard_markdown(script: dict[str, Any], *, title: str = "") -> str:
    """把脚本渲染成**照着拍**用的分镜 md。"""
    shots = _shots(script)
    hooks = _dict_list(script, "hooks")
    covers = _dict_list(script, "covers")
    checks = script.get("shoot_checks") if isinstance(script.get("shoot_checks"), dict) else {}

    lines = [f"# 短视频分镜：{title or script.get('scene') or '（未命名）'}", ""]
    lines.append(f"- 时长：{script.get('duration_s')}s　购物车：{'是' if script.get('cart') else '否'}")
    lines.append(f"- BGM 情绪：{script.get('bgm_mood') or '—'}")
    lines.append("- **本文件仅供拍摄，不发布。** 发布请在 TikTok 由你本人操作。")
    lines += ["", "## 分镜", ""]
    for idx, shot in enumerate(shots, 1):
        lines.append(f"### {idx}. {shot.get('seconds') or ''}｜{shot.get('beat') or ''}")
        lines.append("")
        lines.append(f"- **画面**：{shot.get('visual') or ''}")
        lines.append(f"- **泰语口播**：{shot.get('voice_th') or ''}")
        lines.append(f"- **中文对照**：{shot.get('voice_zh') or ''}")
        lines.append(f"- **泰语字幕**：{shot.get('subtitle_th') or ''}")
        if shot.get("note"):
            lines.append(f"- **备注**：{shot['note']}")
        lines.append("")

    if hooks:
        lines += ["## 开场钩子（三选一）", ""]
        for idx, hook in enumerate(hooks, 1):
            lines.append(f"{idx}. {hook.get('voice_th') or ''}")
            lines.append(f"   中文：{hook.get('voice_zh') or ''}")
        lines.append("")

    if covers:
        lines += ["## 封面文案", ""]
        for idx, cover in enumerate(covers, 1):
            lines.append(f"{idx}. {cover.get('text_th') or ''}（中文：{cover.get('text_zh') or ''}）")
        lines.append("")

    if script.get("teleprompter_th"):
        lines += ["## 提词器（泰语连读）", ""]
        for line in script["teleprompter_th"]:
            lines.append(f"- {line}")
        lines.append("")

    if checks:
        lines += ["## 拍摄自检", ""]
        lines.append(
            f"- 有尺寸参照物：{'✅' if checks.get('has_size_reference') else '❌'}"
        )
        lines.append(
            f"- 无绝对化宣称：{'✅' if checks.get('avoids_absolute_claims') else '❌'}"
        )
        lines.append("")

    if script.get("closing_note"):
        lines += ["## 收尾提示", "", str(script["closing_note"]), ""]
    return "\n".join(lines)


def render_storyboard_csv(script: dict[str, Any]) -> str:
    """分镜 CSV（`\\r\\n` 换行，Excel 友好）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(SHOT_COLUMNS), lineterminator="\r\n")
    writer.writeheader()
    for idx, shot in enumerate(_shots(script), 1):
        writer.writerow(
            {
                "shot_no": idx,
                "seconds": str(shot.get("seconds") or ""),
                "beat": str(shot.get("beat") or ""),
                "visual": str(shot.get("visual") or ""),
                "voice_th": str(shot.get("voice_th") or ""),
                "voice_zh": str(shot.get("voice_zh") or ""),
                "subtitle_th": str(shot.get("subtitle_th") or ""),
                "note": str(shot.get("note") or ""),
            }
        )
    return buffer.getvalue()


def _load_font(size: int):
    """找一个能画泰文的字体；找不到返回 None（调用方降级）。"""
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def _wrap(text: str, font, draw, max_width: int) -> list[str]:
    """按像素宽度折行。泰文没有空格，故**按字符**累积而不是按词切。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        try:
            width = draw.textlength(trial, font=font)
        except Exception:  # noqa: BLE001 - 某些字体无 textlength
            width = len(trial) * (font.size if hasattr(font, "size") else 12)
        if width > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def render_cover_png(
    text: str,
    *,
    size: tuple[int, int] = COVER_SIZE,
    out_path: Path | None = None,
    with_status: bool = False,
) -> bytes | None | tuple[bytes, bool]:
    """把封面文案画成竖版 PNG。返回 PNG 字节；Pillow 不可用时返回 None。

    找不到字体时**仍然出图**（只画背景），并让调用方知道标题没画上 ——
    静默产出一张空白封面比报错更糟，所以返回值由调用方决定怎么提示。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - 环境缺 Pillow
        return None

    width, height = size
    image = Image.new("RGB", size, (18, 18, 20))
    draw = ImageDraw.Draw(image)

    font = _load_font(96)
    drew_text = False
    if font is not None and text.strip():
        lines = _wrap(text.strip(), font, draw, int(width * 0.86))
        line_height = 118
        total = line_height * len(lines)
        y = max(40, (height - total) // 2)
        for line in lines:
            try:
                line_width = draw.textlength(line, font=font)
            except Exception:  # noqa: BLE001
                line_width = len(line) * 48
            draw.text(((width - line_width) / 2, y), line, font=font, fill=(245, 245, 245))
            y += line_height
        drew_text = True

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(payload)
    if with_status:
        # 让调用方**能区分**「画好了」与「只出了一张底图」。
        # 原先只返回 bytes，于是一张纯黑 1080×1920 会被当成可用封面交给卖家 ——
        # 「静默产出一张空白封面比报错更糟」正是本函数 docstring 自己说的话。
        return payload, drew_text
    return payload


def export_script_package(
    script: dict[str, Any],
    out_dir: Path,
    *,
    title: str = "",
) -> dict[str, Any]:
    """把脚本导出成素材包目录，返回产出的文件清单。

    产出：`storyboard.md`、`storyboard.csv`、以及每个封面一张 `cover_N.png`。
    **不发布、不上传、不调平台。**
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    md_path = out_dir / "storyboard.md"
    md_path.write_text(render_storyboard_markdown(script, title=title), encoding="utf-8")
    written.append(md_path.name)

    csv_path = out_dir / "storyboard.csv"
    csv_path.write_text(render_storyboard_csv(script), encoding="utf-8", newline="")
    written.append(csv_path.name)

    covers = _dict_list(script, "covers")
    cover_files: list[str] = []
    blank_covers: list[str] = []
    for idx, cover in enumerate(covers, 1):
        text = str((cover or {}).get("text_th") or "") if isinstance(cover, dict) else ""
        name = f"cover_{idx}.png"
        result = render_cover_png(text, out_path=out_dir / name, with_status=True)
        if result is None:
            continue
        payload, drew_text = result
        if payload is not None:
            cover_files.append(name)
            written.append(name)
            if not drew_text:
                # 底图还是给（比没有强，便于人工补字），但**必须标出来** ——
                # 否则卖家拿到的是一张纯黑 1080×1920 却以为可以直接用。
                blank_covers.append(name)

    notes = ["素材包仅落在本地，不发布、不上传。发布请在 TikTok 由你本人操作。"]
    if blank_covers:
        notes.append(
            "⚠️ 以下封面**没有画上标题**（缺字体或文案为空），是纯色底图，"
            "需人工补字后再用：" + "、".join(blank_covers) + "。"
        )

    return {
        "out_dir": str(out_dir),
        "files": written,
        "covers": cover_files,
        "covers_without_title": blank_covers,
        "shot_count": len(_shots(script)),
        "published": False,
        "uploaded": False,
        "note": " ".join(notes),
    }
