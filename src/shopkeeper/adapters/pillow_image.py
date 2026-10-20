"""本地 Pillow 图像适配器（A3 裁定的 M1 主链路）。

**为什么是本地而不是 API**：A3 实测证明网关唯一通的图像通道是纯文生图，
`/v1/images/edits` 与 `generations+image` 均 400、`/variations` 404（Lead 本人复验），
喂不进 1688 源图。无货源模式下图文必须相符，所以"重绘"降级为**后处理**：
缩放白底补齐 → 检测并遮盖水印 → 检测并抹除源文字带 → 叠加目标语言文案 → 残留检查 → 打标。

与 spike（`spike/image/local_pipeline.py`）的关键差别：
spike 用的是**自造测试图**，水印框和文字带坐标是已知的；
本适配器面对**真实商品图**，位置未知 → 必须做检测，
检测不出来时如实返回降级结果，**绝不假装处理过**。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont, ImageStat

from shopkeeper.adapters.base import ImageClient
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult, ImageAsset

#: 仓库内置 Noto（SIL OFL 1.1）优先；系统字体仅作回退。来源见 docs/fonts.md。
DEFAULT_FONTS: dict[str, tuple[str, ...]] = {
    "EN": ("NotoSans-Regular.ttf", "segoeui.ttf", "arial.ttf", "tahoma.ttf", "DejaVuSans.ttf"),
    "VI": ("NotoSans-Regular.ttf", "tahoma.ttf", "arial.ttf", "DejaVuSans.ttf"),
    "TH": ("NotoSansThai-Regular.ttf", "Tahoma.ttf", "Leelawadee UI.ttf", "sarabun.ttf"),
    "CJK": ("NotoSansSC-Subset.ttf", "msyh.ttc", "simhei.ttf", "NotoSansCJK-Regular.ttc"),
}
#: 泰语在本机无 Raqm 排版引擎时声调符位置不可保证（A3 实测）→ 仍转人工，与是否有字形无关
HUMAN_REVIEW_LANGS = {"TH"}


def bundled_font_dir() -> Path:
    """随包分发的开源字体目录（macOS / Windows / Linux CI 共用）。"""
    return Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _font_dir() -> Path:
    """兼容旧测试名：主字体目录 = 内置目录。"""
    return bundled_font_dir()


def system_font_dirs() -> list[Path]:
    dirs: list[Path] = []
    for key in ("SYSTEMROOT", "WINDIR"):
        root = os.environ.get(key)
        if root:
            dirs.append(Path(root) / "Fonts")
            break
    dirs.extend(
        [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local" / "share" / "fonts",
        ]
    )
    # 去重且只留存在的目录
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            resolved = d.resolve()
        except OSError:
            continue
        if resolved in seen or not d.is_dir():
            continue
        seen.add(resolved)
        out.append(d)
    return out


def resolve_font_file(names: tuple[str, ...] | list[str]) -> Path | None:
    """按名字在内置目录与系统字体目录中查找，内置优先。"""
    search = [bundled_font_dir(), *system_font_dirs()]
    for name in names:
        for folder in search:
            direct = folder / name
            if direct.is_file():
                return direct
            # Linux 字体常按家族分子目录；只在系统目录里按 basename 浅搜一层
            if folder == bundled_font_dir():
                continue
            try:
                for child in folder.iterdir():
                    if child.is_file() and child.name.lower() == name.lower():
                        return child
                    if child.is_dir():
                        hit = child / name
                        if hit.is_file():
                            return hit
            except OSError:
                continue
    return None


class PillowImageClient(ImageClient):
    name = "pillow_image"

    def __init__(
        self,
        *,
        output_dir: str | os.PathLike[str] = "artifacts/demo/images",
        target_size: int = 1000,
        watermark_corner_ratio: float = 0.34,
        lang_fonts: dict[str, tuple[str, ...]] | None = None,
        copy_provider: Any | None = None,
        fail_for_test: AdapterError | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.target_size = int(target_size)
        self.watermark_corner_ratio = float(watermark_corner_ratio)
        self.lang_fonts = lang_fonts or DEFAULT_FONTS
        self.copy_provider = copy_provider  # callable(lang) -> {"headline","sub"}
        self._fail = fail_for_test
        self.timeout_s = float(timeout_s)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 入口

    def repaint(
        self,
        image_ref,
        lang,
        *,
        watermark_check=True,
        overlay_headline=None,
        overlay_sub=None,
    ) -> AdapterResult[ImageAsset]:
        started = time.monotonic()
        if self._fail is not None:
            return AdapterResult.failure(
                AdapterError(
                    code=self._fail.code, kind=self._fail.kind, message=self._fail.message,
                    retryable=self._fail.retryable, human_todo=True,
                    detail={"fallback": "original_kept", "source_ref": image_ref,
                            "needs_human": True, **(self._fail.detail or {})},
                ),
                self.name, int((time.monotonic() - started) * 1000),
            )
        if not image_ref:
            return AdapterResult.failure(AdapterError.validation("IMG_NO_REF", "缺少源图引用"), self.name)
        lang = (lang or "").upper()
        if not lang:
            return AdapterResult.failure(AdapterError.validation("IMG_NO_LANG", "缺少目标语种"), self.name)

        loaded = self._load(image_ref)
        if isinstance(loaded, AdapterError):
            return AdapterResult.failure(loaded, self.name, int((time.monotonic() - started) * 1000))
        img, provenance = loaded

        copy = self._copy_for(lang, overlay_headline, overlay_sub)
        if copy is None:
            return AdapterResult.failure(
                AdapterError.validation("IMG_NO_COPY", f"没有 {lang} 的可叠加文案（overlay_headline 为空）"),
                self.name, int((time.monotonic() - started) * 1000),
            )
        placeholder_copy = not (overlay_headline or "").strip()

        font_path, font_err = self._pick_font(lang)
        if font_err is not None:
            return AdapterResult.failure(font_err, self.name, int((time.monotonic() - started) * 1000))

        square = self._normalize_square(img)
        wm_box = self._detect_watermark(square)
        # 水印区不参与文字带判定：红色角标转灰度后是深色，会被误判成"文字带"，
        # 导致同一块像素既当水印又当文字带（诊断实测到 band_box 正好等于角标区）。
        band_box = self._detect_text_band(square, exclude=wm_box)
        # 绝不产出"半翻译图"。真实商品图的文字常散落多处（标题 + 步骤1/2/3/4 + 角标），
        # 只擦一条带再叠英文 = 其余中文原样留着，比完全不动更糟（看着像处理过了）。
        # 实测踩过：一张转印贴教程图只擦掉了标题「使用教程」的上半，6 行中文全留。
        # 判据：所选带须覆盖全图 ≥80% 含字行，否则整图不动、不叠字，并记告警。
        overlay_unsafe = False
        if band_box is not None and not self._band_covers_text(square, band_box, exclude=wm_box):
            band_box = None
            overlay_unsafe = True

        out = square.copy()
        rendered: dict | None = None
        overlay_failed = False
        d = ImageDraw.Draw(out)
        covered = None
        if wm_box is not None:
            covered = self._cover_region(d, out, wm_box)
        erased = None
        if band_box is not None:
            erased = self._cover_region(d, out, band_box, fill=(246, 246, 246))
            rendered = self._overlay_text(out, band_box, copy, font_path)
            if rendered is None:
                # 带已擦掉但字没画上 = 图上信息被抹了却没补上，比不擦更糟。
                # 必须显式记一笔并转人工，不能让 copy_rendered=False 冒充"本来就没文字"。
                overlay_failed = True

        missing_glyphs = self._tofu_chars(font_path, copy)
        detected = wm_box is not None or band_box is not None
        residue = self._residue_check(square, out, wm_box) if (watermark_check and wm_box) else None

        digest = hashlib.sha256(f"{image_ref}|{lang}|{provenance}".encode("utf-8")).hexdigest()[:16]
        path = self.output_dir / f"{digest}_{lang}.png"
        meta = self._save_tagged(out, path, image_ref, ai_generated=False)

        # ---- 人工闸门判定 ----
        # 关键区分：检测器只认"右下角红/橙角标"这一种形态。
        # "没找到" ≠ "确认无水印"，但也 ≠ "这张图必须人工看"——
        # 把它塞进 needs_human 会让每张干净图都转人工（实测 10/10 全中），
        # 标记恒真等于没有标记，真正的风险（泰语字形、遮盖残留）反被噪声埋掉。
        # 所以 not_found 只作为**可见告警**记录，不触发人工闸门。
        warnings: list[str] = []
        needs_human = (
            lang in HUMAN_REVIEW_LANGS
            or bool(missing_glyphs)
            or bool(residue and not residue["passed"])
            or placeholder_copy
            or overlay_failed
            or overlay_unsafe  # 图上有源文字但我们判定整带擦除不安全 → 绝不能自动放行
        )
        reasons: list[str] = []
        if overlay_unsafe:
            reasons.append("图上源文字散落多处、无法整带擦除 → 未叠目标语言（避免半翻译图），须人工处理")
        if overlay_failed:
            reasons.append("已擦除源文字带但目标文案未画上 → 图上信息缺失，必须人工处理")
        if placeholder_copy:
            reasons.append("图上文字是占位串（调用方未传目标语言文案）→ 该图不可上架")
        if lang in HUMAN_REVIEW_LANGS:
            reasons.append("TH 无 Raqm 排版引擎，声调符位置不可保证（A3 实测）")
        if missing_glyphs:
            reasons.append(f"字体缺字: {missing_glyphs[:6]}")
        if residue and not residue["passed"]:
            reasons.append(f"水印残留检查未过: {residue}")
        if not detected:
            warnings.append(
                "未检出水印/源文字带（检测器只覆盖右下角红橙角标）→ 未做遮盖；"
                "原图若带其它形态水印（浅色文字、居中logo、平铺），本轮检不出"
            )
        if residue and residue["passed"]:
            wm_state = "passed"
        elif residue:
            wm_state = "failed"
        elif wm_box is None and band_box is None:
            wm_state = "not_found"  # 查过了，没有可擦的东西
        else:
            wm_state = "not_implemented"

        return AdapterResult.success(
            ImageAsset(
                source_ref=image_ref,
                output_path=str(path),
                mode="post_processed",
                needs_human=needs_human,
                watermark_check=wm_state,
                latency_ms=int((time.monotonic() - started) * 1000),
                cost_amount=None,  # 本地零成本，不编数字
                notes={
                    "engine": "pillow-postprocess-v1",
                    "provenance": provenance,
                    "watermark_box": list(wm_box) if wm_box else None,
                    "text_band_box": list(band_box) if band_box else None,
                    "cover_color": list(covered) if covered else None,
                    "erased_box": list(erased) if erased else None,
                    "font": font_path.name,
                    "copy": copy,
                    "copy_is_placeholder": placeholder_copy,
                    "text_render": rendered,
                    # 真实商品图常常本来就没有中文文字带 → 没有需要替换的图上文案。
                    # 必须把这个区分写进留痕，否则"干净图"会被误读成"已翻译文案的图"。
                    "copy_rendered": rendered is not None,
                    "overlay_failed": overlay_failed,
                    "no_copy_reason": (
                        None if rendered is not None
                        else "未检出源文字带 → 该图无需替换图上文案（不是漏做）"
                    ),
                    "missing_glyphs": missing_glyphs,
                    "needs_human_reasons": reasons,
                    "warnings": warnings,
                    "residue": residue,
                    "ai_tag": meta,
                },
            ),
            self.name, int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------------ 载入

    def _load(self, ref: str):
        """支持本地路径 / http(s) URL / mock:// 占位图。返回 (img, provenance) 或 AdapterError。"""
        if ref.startswith("mock://") or ref.startswith("placeholder:"):
            seed = hashlib.sha256(ref.encode("utf-8")).digest()
            img = Image.new("RGB", (900, 1200), (252, 252, 252))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((300, 320, 600, 980), radius=40, fill=(150 + seed[0] % 60, 155, 165))
            # 顶部中文卖点带：**两行 56px 大字**，密度对齐真实 1688 卖点带。
            # 踩过的坑：先用默认点阵字、再用单行 44px，暗像素占比都只有 ~1 行过 8% 阈值，
            # 检测判"无文字带" → "擦除+叠字"这条主路径在测试里静默不执行（假绿）。
            d.rectangle((0, 30, 900, 190), fill=(245, 245, 245))
            band_font = self._pick_font("CJK")
            drawn = False
            if band_font[1] is None:
                try:
                    # 内置 Noto SC 子集笔画比 Windows 微软雅黑细；字号略加大并描边，
                    # 保证行暗像素占比仍落在文字带检测的 8%~40% 窗口（否则夹具假绿）。
                    f = ImageFont.truetype(str(band_font[0]), 64)
                    kw = {"font": f, "fill": (10, 10, 10), "stroke_width": 1, "stroke_fill": (10, 10, 10)}
                    d.text((24, 36), "长效保温24小时 316不锈钢", **kw)
                    d.text((24, 108), "500ml大容量 一键开盖", **kw)
                    drawn = True
                except Exception:  # noqa: BLE001
                    drawn = False
            if not drawn:
                # 环境缺 CJK 字体时退回实心条：保证"浅底+深色块"的带结构仍在，
                # 否则夹具会因缺字体而悄悄跳过主路径（又是假绿）。
                d.rectangle((24, 52, 860, 104), fill=(30, 30, 30))
                d.rectangle((24, 124, 700, 176), fill=(30, 30, 30))
            d.rectangle((700, 1080, 880, 1160), fill=(255, 43, 43))
            d.text((716, 1096), "1688", fill=(255, 255, 255))
            return img, "mock"
        if ref.startswith(("http://", "https://")):
            try:
                r = requests.get(ref, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://detail.1688.com/"}, timeout=self.timeout_s)
            except (requests.RequestException, OSError) as exc:
                return AdapterError.generation("IMG_FETCH_FAILED", f"{type(exc).__name__}: {exc}"[:180])
            if r.status_code != 200 or len(r.content) < 512:
                return AdapterError.generation("IMG_FETCH_STATUS", f"status={r.status_code} bytes={len(r.content)}")
            try:
                import io

                return Image.open(io.BytesIO(r.content)).convert("RGB"), "remote"
            except Exception as exc:  # noqa: BLE001
                return AdapterError.generation("IMG_NOT_DECODABLE", f"{type(exc).__name__}: {exc}"[:160])
        p = Path(ref)
        if not p.exists():
            return AdapterError.generation("IMG_FILE_MISSING", f"本地文件不存在: {ref}")
        try:
            return Image.open(p).convert("RGB"), "local"
        except Exception as exc:  # noqa: BLE001
            return AdapterError.generation("IMG_NOT_DECODABLE", f"{type(exc).__name__}: {exc}"[:160])

    # ------------------------------------------------------------------ 处理

    def _normalize_square(self, img: Image.Image) -> Image.Image:
        im = img.copy()
        im.thumbnail((self.target_size, self.target_size), Image.LANCZOS)
        out = Image.new("RGB", (self.target_size, self.target_size), (255, 255, 255))
        out.paste(im, ((self.target_size - im.width) // 2, (self.target_size - im.height) // 2))
        return out

    def _cover_region(self, d: ImageDraw.ImageDraw, img: Image.Image, box, fill=None):
        x0, y0, x1, y1 = box
        if fill is None:
            # 用区域上边界那条背景带的中位数色，遮盖痕迹比纯白自然
            border = img.crop((x0, max(0, y0 - 12), x1, max(1, y0))).convert("RGB").tobytes()
            if border:
                fill = tuple(sorted(border[i::3])[len(border) // 6] for i in range(3))
            else:
                fill = (255, 255, 255)
        d.rectangle(box, fill=fill)
        return fill

    def evaluate_corner_watermark(self, image_path: str | os.PathLike[str]) -> dict[str, Any]:
        """重绘后必跑的角标水印检测（只读，不改图）。

        检测器只覆盖右下角红/橙角标。`not_found` → 写 warnings，**不**计入 needs_human
        （M1→M2 边界：检不出 ≠ 确认无水印，也不等于必须人工）。
        """
        path = Path(image_path)
        warnings: list[str] = []
        if not path.is_file():
            return {
                "watermark_check": "failed",
                "watermark_box": None,
                "residue": None,
                "warnings": ["重绘产物路径不存在，无法做水印检测"],
                "needs_human_from_watermark": True,
            }
        try:
            img = Image.open(path).convert("RGB")
        except OSError:
            return {
                "watermark_check": "failed",
                "watermark_box": None,
                "residue": None,
                "warnings": ["重绘产物不可解码，无法做水印检测"],
                "needs_human_from_watermark": True,
            }
        wm_box = self._detect_watermark(img)
        if wm_box is None:
            warnings.append(
                "未检出水印（检测器只覆盖右下角红橙角标）→ 检不出即放行并写 warnings，"
                "不计入 needs_human；浅色文字/居中 logo/平铺水印本轮检不出"
            )
            return {
                "watermark_check": "not_found",
                "watermark_box": None,
                "residue": None,
                "warnings": warnings,
                "needs_human_from_watermark": False,
            }
        # 重绘产物上仍检出角标：记 failed + 转人工（未再遮盖）
        return {
            "watermark_check": "failed",
            "watermark_box": list(wm_box),
            "residue": None,
            "warnings": warnings,
            "needs_human_from_watermark": True,
            "needs_human_reasons": ["重绘后仍检出右下角红橙角标，须人工复核"],
        }

    def _detect_watermark(self, img: Image.Image):
        """真实图水印位置未知 → 在右下角区域找**高饱和红/橙色块**（1688 角标的常见形态）。

        找不到就返回 None；上层写 warnings，不假装遮盖过，也不因此强制 needs_human。
        """
        w, h = img.size
        side = max(40, int(w * self.watermark_corner_ratio))
        region_box = (w - side, h - side, w, h)
        region = img.crop(region_box).convert("RGB")
        flat = region.tobytes()  # Pillow 14 移除 getdata()，用字节流自己切三元组
        px = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)]
        best_area = 0
        best_box = None
        # 粗网格扫描：找连续红/橙饱和块的最大外接框
        step = 8
        rw, rh = region.size
        mask = [[self._is_badge_color(px[y * rw + x]) for x in range(0, rw, step)] for y in range(0, rh, step)]
        xs = [x for row in mask for v, x in zip(row, range(0, rw, step)) if v]
        ys = [y for ri, row in enumerate(mask) for v in row if v for y in [ri * step]]
        if len(xs) >= 12 and len(ys) >= 6:
            best_area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            if best_area >= 900:
                best_box = (
                    region_box[0] + max(0, min(xs) - 10),
                    region_box[1] + max(0, min(ys) - 10),
                    min(w, region_box[0] + max(xs) + step + 10),
                    min(h, region_box[1] + max(ys) + step + 10),
                )
        return best_box

    @staticmethod
    def _is_badge_color(rgb) -> bool:
        r, g, b = rgb[0], rgb[1], rgb[2]
        return r > 170 and g < 120 and b < 120 and (r - max(g, b)) > 60

    def _band_covers_text(self, img: Image.Image, band_box: tuple, exclude=None, threshold: float = 0.8) -> bool:
        """所选带是否覆盖**全图绝大多数**含字行。

        真实商品图的文字常散落多处（标题 + 步骤1/2/3/4 + 角标）。只擦一条带再叠英文
        = 其余中文原样留着 → 半翻译图，比完全不动更糟（看着像处理过了）。
        覆盖不足则整图不动、不叠字，并转人工。

        `exclude` 必须传水印框：红角标转灰度后也是深色像素，不排除的话
        水印行会被算进"未被带覆盖的文字行"，导致**任何带角标的图都永远判不安全**
        （我自己引入过这个回归，测试当场红了）。
        """
        w, h = img.size
        small = img.convert("L").resize((min(w, 200), h), Image.BILINEAR)
        sw, sh = small.size
        px = list(small.tobytes())
        ex = (exclude[1], exclude[3]) if exclude else None
        text_rows = []
        for y in range(sh):
            if ex and ex[0] - 2 <= y <= ex[1] + 2:
                continue  # 水印区不参与覆盖率判定
            row = px[y * sw : (y + 1) * sw]
            if not row:
                continue
            dark = sum(1 for v in row if v < 120) / len(row)
            median = sorted(row)[len(row) // 2]
            if 0.08 < dark <= 0.40 and median >= 200:
                text_rows.append(y)
        if not text_rows:
            return False
        scale = h / sh
        b0, b1 = band_box[1] / scale, band_box[3] / scale
        inside = sum(1 for y in text_rows if b0 - 2 <= y <= b1 + 2)
        return inside / len(text_rows) >= threshold
        """所选带是否覆盖了**全图绝大多数**含字行。

        真实商品图的文字常常散落多处（标题、步骤 1/2/3/4、角标…）。
        只擦一条带再叠英文 = 其余中文原样留着 → 半翻译图，比不动更糟。
        覆盖不足就整图不动、转人工，绝不产出半翻译产物。
        """
        w, h = img.size
        small = img.convert("L").resize((min(w, 200), h), Image.BILINEAR)
        sw, sh = small.size
        px = list(small.tobytes())
        text_rows = []
        for y in range(sh):
            row = px[y * sw : (y + 1) * sw]
            if not row:
                continue
            dark = sum(1 for v in row if v < 120) / len(row)
            median = sorted(row)[len(row) // 2]
            if 0.08 < dark <= 0.40 and median >= 200:
                text_rows.append(y)
        if not text_rows:
            return False
        scale = h / sh
        b0, b1 = band_box[1] / scale, band_box[3] / scale
        inside = sum(1 for y in text_rows if b0 - 2 <= y <= b1 + 2)
        return inside / len(text_rows) >= threshold

    def _detect_text_band(self, img: Image.Image, exclude=None):
        """找顶部或底部的**文字带**：按行统计暗像素占比，取连续高占比行。

        两条判据缺一不可（都是看图改出来的，不是推的）：
        ① 行暗像素占比在 **8%~40%**：文字带是"浅底上零星深色笔画"，
           而商品本体（牛仔裤、黑鞋）会让暗像素占比冲到 50%+；
        ② 行**中位亮度 ≥ 200**：文字带底色是白/浅灰。
           首跑用 ≥170 仍误判，因为深色商品横贯半行时中位数正好卡在边界上。
        找不到返回 None（真实图常常没有中文带，这是正常情况，不是失败）。
        """
        w, h = img.size
        gray = img.convert("L")
        small = gray.resize((min(w, 200), h), Image.BILINEAR)
        sw, sh = small.size
        px = list(small.tobytes())  # 灰度：一字节一像素

        def row_stats(y):
            row = px[y * sw : (y + 1) * sw]
            if not row:
                return 0.0, 255
            dark = sum(1 for v in row if v < 120)
            return dark / max(1, len(row)), sorted(row)[len(row) // 2]

        ratios = [row_stats(y) for y in range(sh)]

        # 行间断档容忍：多行卖点的两行之间必有空白。旧实现"遇空白即断"，
        # 于是两行中文被当成两条独立带，而下面只取最高的一条
        # → 第二行中文原样留在图上，产出成了"英文叠在上面、中文还在下面"的半翻译图。
        # 容差取行高量级（~3% 图高），足以并起同区多行，又不会跨到商品本体。
        gap_tol = max(6, int(sh * 0.03))

        def band(rows):
            runs, cur, gap = [], [], 0
            for y, (dark_ratio, median) in rows:
                if 0.08 < dark_ratio <= 0.40 and median >= 200:
                    cur.append(y)
                    gap = 0
                elif cur and gap < gap_tol:
                    gap += 1  # 短空白：可能是两行之间，先不断
                else:
                    if len(cur) >= 3:
                        runs.append((cur[0], cur[-1]))
                    cur, gap = [], 0
            if len(cur) >= 3:
                runs.append((cur[0], cur[-1]))
            return runs

        top = band([(y, ratios[y]) for y in range(min(sh, int(sh * 0.22)))])
        bottom = band([(y, ratios[y]) for y in range(int(sh * 0.78), sh)])
        cands = [r for r in (top + bottom) if (r[1] - r[0]) >= 12]
        if exclude:
            ex0, ey0, ex1, ey1 = exclude

            def overlaps(r):
                y0, y1 = r
                inter = max(0, min(y1, ey1) - max(y0, ey0))
                return inter >= 0.5 * max(1, (y1 - y0))

            cands = [r for r in cands if not overlaps(r)]
        if not cands:
            return None
        y0, y1 = max(cands, key=lambda r: r[1] - r[0])
        scale = h / sh
        return (0, max(0, int(y0 * scale) - 6), w, min(h, int((y1 + 1) * scale) + 6))

    def _overlay_text(self, img: Image.Image, band_box, copy: dict, font_path: Path) -> None:
        """叠加目标语言文案，**按画布宽度自适应**：先缩字号，缩到底仍超宽就折行。

        首跑踩的坑：泰语标题按固定字号直排，超出右边界被硬截断
        （"เสื้อกล้ามลูกไม้ห" 半句话）。截断的文案比没有文案更糟。
        """
        d = ImageDraw.Draw(img)
        x0, y0, _x1, y1 = band_box
        band_h = max(28, y1 - y0)
        avail_w = max(60, img.width - x0 - 28)
        headline = (copy.get("headline") or "").strip()
        sub = (copy.get("sub") or "").strip()

        def fit(text: str, start: int, min_size: int, max_lines: int):
            size = start
            while size > min_size:
                try:
                    f = ImageFont.truetype(str(font_path), size)
                except Exception:  # noqa: BLE001
                    return size, [text]
                if d.textlength(text, font=f) <= avail_w:
                    return size, [text]
                size = int(size * 0.88)
            try:
                f = ImageFont.truetype(str(font_path), min_size)
            except Exception:  # noqa: BLE001
                return min_size, [text]
            lines, cur = [], ""
            for word in text.split():
                trial = f"{cur} {word}".strip()
                if cur and d.textlength(trial, font=f) > avail_w:
                    lines.append(cur)
                    cur = word
                    if len(lines) >= max_lines:
                        break
                else:
                    cur = trial
            if cur and len(lines) < max_lines:
                lines.append(cur)
            return min_size, (lines or [text[:40]])

        y = y0 + 4
        h_size, h_lines = fit(headline, max(14, min(40, int(band_h * 0.42))), 15, 2)
        try:
            f_h = ImageFont.truetype(str(font_path), h_size)
        except Exception:  # noqa: BLE001
            return
        rendered: list[tuple[str, int]] = []
        for line in h_lines:
            d.text((x0 + 14, y), line, font=f_h, fill=(20, 20, 20))
            rendered.append((line, h_size))
            y += h_size + 4
        if sub:
            s_size, s_lines = fit(sub, max(11, int(h_size * 0.72)), 11, 2)
            try:
                f_s = ImageFont.truetype(str(font_path), s_size)
            except Exception:  # noqa: BLE001
                f_s = f_h
                s_size = h_size
            for line in s_lines:
                d.text((x0 + 14, y), line, font=f_s, fill=(90, 90, 90))
                rendered.append((line, s_size))
                y += s_size + 3
        # 返回渲染记录，不挂到 self 上：适配器必须无状态（DESIGN §2.1 原则 2），
        # 挂在 self 上会跨调用泄漏，并发时更会串台。
        return {"lines": [len(ln) for ln, _ in rendered], "sizes": [s for _, s in rendered]}

    def _residue_check(self, src: Image.Image, proc: Image.Image, box):
        stat = ImageStat.Stat(proc.convert("L").crop(box))
        stddev = round(stat.stddev[0], 2)
        dist = self._dhash_dist(self._dhash(src, box), self._dhash(proc, box))
        return {"corner_stddev": stddev, "dhash_dist": dist, "passed": stddev < 6.0 and dist >= 10}

    @staticmethod
    def _dhash(img, box, size=8):
        g = img.convert("L").crop(box).resize((size + 1, size), Image.LANCZOS)
        px = list(g.tobytes())
        return [1 if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else 0 for r in range(size) for c in range(size)]

    @staticmethod
    def _dhash_dist(a, b):
        return sum(x != y for x, y in zip(a, b))

    # ------------------------------------------------------------------ 辅助

    def _copy_for(self, lang: str, headline: str | None, sub: str | None):
        """图上文字来自 Stylist 传入的目标语言文案；没传就是占位假字，必须标出来。"""
        h = (headline or "").strip()
        if not h and self.copy_provider is not None:
            got = self.copy_provider(lang)
            if got and got.get("headline"):
                return got
            return None
        if not h:
            # 允许无文案跑通链路（契约/冒烟测试用），但上层会因 placeholder 标 needs_human
            return {"headline": f"[placeholder {lang}]", "sub": (sub or "").strip()}
        return {"headline": h, "sub": (sub or "").strip()}

    def _pick_font(self, lang: str):
        names = tuple(self.lang_fonts.get(lang, self.lang_fonts.get("EN", ())))
        found = resolve_font_file(names)
        if found is not None:
            return found, None
        return None, AdapterError.generation(
            "IMG_FONT_MISSING",
            f"{lang} 无可用字体（找过 {names}；内置目录 {bundled_font_dir()}）→ 该语种图必须人工处理",
            font_dir=str(bundled_font_dir()),
        )

    def _tofu_chars(self, font_path: Path, copy: dict) -> list[str]:
        try:
            font = ImageFont.truetype(str(font_path), 24)
            base = font.getmask(chr(0xE005), mode="L")
            base_sig = (base.size, bytes(base))
        except Exception:  # noqa: BLE001
            return []
        text = (copy.get("headline") or "") + (copy.get("sub") or "")
        out = []
        for ch in set(text):
            if not ch.strip():
                continue
            try:
                m = font.getmask(ch, mode="L")
                if (m.size, bytes(m)) == base_sig:
                    out.append(ch)
            except Exception:  # noqa: BLE001
                continue
        return sorted(out)

    def _save_tagged(self, img: Image.Image, path: Path, source_ref: str, *, ai_generated: bool) -> dict:
        from PIL.PngImagePlugin import PngInfo

        meta = {
            # 本地后处理不是 AI 生成图像内容，但仍是机器加工产物：如实标 pipeline
            "ai_generated": "true" if ai_generated else "false",
            "pipeline": "pillow-postprocess-v1",
            "source_ref": source_ref[:200],
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        info = PngInfo()
        for k, v in meta.items():
            info.add_text(k, v)
        img.save(path, pnginfo=info)
        return meta
