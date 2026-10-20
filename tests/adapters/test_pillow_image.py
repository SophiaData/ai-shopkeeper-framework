"""PillowImageClient 测试：契约 + 真实检测行为。

分两层：
- 离线层用 `mock://` 合成图（含假 1688 红角标 + 中文带），CI 无网络可跑；
- 真实层用 A3 spike 下载的真 1688 CDN 图（cbu01.alicdn.com **未被反爬**，与详情页不同），
  网关/CDP 不可达时自动跳过。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import requests
from PIL import Image

from shopkeeper.adapters.contract_suite import assert_image_contract
from shopkeeper.adapters.pillow_image import (
    DEFAULT_FONTS,
    PillowImageClient,
    bundled_font_dir,
    resolve_font_file,
)
from shopkeeper.errors import AdapterError, ErrorKind


def _bundled_fonts_present() -> bool:
    """仓库内置 Noto 覆盖 EN/VI/TH/CJK；缺文件才跳过（不应在正常检出里发生）。"""
    return all(resolve_font_file(DEFAULT_FONTS[lang]) is not None for lang in ("EN", "VI", "TH", "CJK"))


_NEEDS_FONTS = pytest.mark.skipif(
    not _bundled_fonts_present(),
    reason=(
        "仓库内置字体缺失（应在 src/shopkeeper/assets/fonts/）。"
        f"当前目录 {bundled_font_dir()}。"
        "见 docs/fonts.md。"
    ),
)


def _ink_ratio(path: Path) -> float:
    """非近白像素占比。豆腐块或空画会显著偏低。"""
    img = Image.open(path).convert("L")
    px = list(img.tobytes())
    if not px:
        return 0.0
    return sum(1 for v in px if v < 245) / len(px)

REAL_IMG = "https://cbu01.alicdn.com/img/ibank/O1CN01LQYpAk1NWJdNONL5Z_!!<seller-id-redacted>-0-cib.jpg"


@pytest.fixture()
def client(tmp_path):
    return PillowImageClient(output_dir=tmp_path / "imgs", target_size=600)


def headline_kwargs():
    return {"overlay_headline": "STAYS HOT 24H", "overlay_sub": "316 steel 500ml"}


def clean_png(tmp_path, name="clean.png"):
    """一张干净图：无角标、无文字带（真实 CDN 商品图多数长这样）。"""
    p = tmp_path / name
    Image.new("RGB", (600, 600), (250, 250, 250)).save(p)
    return str(p)


# ------------------------------------------------- "查了没找到" ≠ "必须人工看"

@_NEEDS_FONTS
def test_clean_image_is_not_found_and_does_not_need_human(tmp_path):
    """钉住一个真实教训：旧实现把"检测器没找到水印"当成"不知道"，
    结果 10/10 真实商品图全被标 needs_human —— 标记恒真等于没有标记，
    真正该看的（泰语字形、遮盖残留）反被噪声淹没。

    非 TH 语种 + 干净图 → 放行，但检测器覆盖面有限这件事必须留在 warnings 里。
    """
    c = PillowImageClient(output_dir=tmp_path / "o", target_size=400)
    res = c.repaint(clean_png(tmp_path), "VI", overlay_headline="Giữ nhiệt 24 giờ", overlay_sub="Thép 316")
    assert res.ok, res.error
    assert res.data.watermark_check == "not_found"
    assert res.data.needs_human is False, "干净图不该转人工"
    assert res.data.notes["warnings"], "检测器只覆盖角标形态，必须留下告警"
    assert "检不出" in res.data.notes["warnings"][0]


@_NEEDS_FONTS
def test_thai_needs_human_on_clean_image_for_glyph_reason_not_watermark(tmp_path):
    """TH 转人工的理由必须是字形风险，不能是"没找到水印"——
    否则同一个布尔值混着两种原因，抽检的人无从判断该看什么。"""
    c = PillowImageClient(output_dir=tmp_path / "o2", target_size=400)
    res = c.repaint(clean_png(tmp_path, "clean2.png"), "TH",
                    overlay_headline="เก็บความร้อนได้นาน", overlay_sub="สแตนเลส 316")
    assert res.ok
    assert res.data.needs_human is True
    reasons = res.data.notes["needs_human_reasons"]
    assert any("Raqm" in r for r in reasons), reasons
    assert all("水印" not in r for r in reasons), f"水印原因不该进闸门: {reasons}"
    assert res.data.watermark_check == "not_found"


# ------------------------------------------------------------------ 契约

@_NEEDS_FONTS
def test_passes_shared_image_contract(tmp_path):
    assert_image_contract(lambda: PillowImageClient(output_dir=tmp_path / "c", target_size=500))


# ------------------------------------------------------------------ 真实处理

@_NEEDS_FONTS
def test_produces_openable_png_with_ai_tag(client):
    res = client.repaint("mock://product-1", "EN", **headline_kwargs())
    assert res.ok, res.error
    path = Path(res.data.output_path)
    assert path.exists(), "必须真的落盘"
    img = Image.open(path)
    img.load()
    assert img.size == (600, 600), "应归一到目标方图"
    assert img.info.get("ai_generated") in ("true", "false")
    assert img.info.get("pipeline") == "pillow-postprocess-v1"
    assert img.info.get("source_ref") == "mock://product-1"


@_NEEDS_FONTS
def test_detects_and_covers_synthetic_watermark(client):
    """mock:// 图右下角有刻意画的红角标 → 必须被检出并遮盖，而不是假装处理过。"""
    res = client.repaint("mock://badge-test", "EN", **headline_kwargs())
    assert res.ok
    notes = res.data.notes
    assert notes["watermark_box"], f"未检出合成水印: {notes}"
    assert notes["residue"]["passed"] is True, f"遮盖后仍有残留: {notes['residue']}"
    assert res.data.watermark_check == "passed"


@_NEEDS_FONTS
def test_corner_is_clean_after_cover(client):
    res = client.repaint("mock://badge-test", "EN", **headline_kwargs())
    img = Image.open(res.data.output_path).convert("RGB")
    w, h = img.size
    corner = img.crop((int(w * 0.72), int(h * 0.72), w, h))
    flat = corner.tobytes()
    px = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)]
    red = sum(1 for r, g, b in px if r > 170 and g < 120 and b < 120)
    assert red == 0, f"右下角仍检出 {red} 个红色像素 → 水印没盖住"


@_NEEDS_FONTS
def test_placeholder_copy_is_flagged_not_deliverable(client):
    """没传目标语言文案时图上会写 [placeholder TH] —— 这种图绝不能当成品。"""
    res = client.repaint("mock://p1", "VI")
    assert res.ok
    assert res.data.notes["copy_is_placeholder"] is True
    assert res.data.needs_human is True
    assert any("占位" in r for r in res.data.notes["needs_human_reasons"])


@_NEEDS_FONTS
def test_real_copy_is_not_placeholder(client):
    res = client.repaint("mock://p2", "VI", **headline_kwargs())
    assert res.ok and res.data.notes["copy_is_placeholder"] is False


@_NEEDS_FONTS
def test_thai_always_needs_human(client):
    """A3 实测：本机无 Raqm，泰语声调符位置不可保证 → 必须人工。"""
    res = client.repaint("mock://th1", "TH", overlay_headline="เก็บความร้อนได้นาน", overlay_sub="สแตนเลส 316")
    assert res.ok
    assert res.data.needs_human is True
    assert any("Raqm" in r or "泰" in r or "TH" in r for r in res.data.notes["needs_human_reasons"])
    assert res.data.notes["missing_glyphs"] == [], f"泰语字形应齐全（Noto Sans Thai）: {res.data.notes['missing_glyphs']}"
    assert _ink_ratio(Path(res.data.output_path)) > 0.02, "泰语图几乎无墨迹，疑似豆腐块或未绘制"


@_NEEDS_FONTS
def test_vietnamese_diacritics_have_glyphs(client):
    res = client.repaint("mock://vi1", "VI", overlay_headline="Giữ nhiệt đến 24 giờ", overlay_sub="Thép không gỉ")
    assert res.ok
    assert res.data.notes["missing_glyphs"] == [], f"越南语变音符缺字: {res.data.notes['missing_glyphs']}"
    assert res.data.needs_human is False, "越南语可自动放行（A3 实测结论）"
    assert _ink_ratio(Path(res.data.output_path)) > 0.02, "越南语图几乎无墨迹，疑似豆腐块或未绘制"


@_NEEDS_FONTS
def test_thai_and_vietnamese_overlay_are_not_tofu_boxes(tmp_path):
    """在干净底图上叠 TH/VI：字形掩码不得等于未定义码位（豆腐），且有可见墨迹。"""
    from PIL import ImageDraw, ImageFont

    c = PillowImageClient(output_dir=tmp_path / "glyphs", target_size=400)
    cases = [
        ("TH", "เก็บความร้อนได้นาน", "สแตนเลส 316"),
        ("VI", "Giữ nhiệt đến 24 giờ", "Thép không gỉ"),
    ]
    for lang, headline, sub in cases:
        res = c.repaint(clean_png(tmp_path, f"{lang}.png"), lang, overlay_headline=headline, overlay_sub=sub)
        assert res.ok, (lang, res.error)
        assert res.data.notes["missing_glyphs"] == [], (lang, res.data.notes["missing_glyphs"])
        font_path = resolve_font_file(DEFAULT_FONTS[lang])
        assert font_path is not None
        font = ImageFont.truetype(str(font_path), 48)
        canvas = Image.new("RGB", (800, 200), (255, 255, 255))
        ImageDraw.Draw(canvas).text((20, 40), headline, font=font, fill=(0, 0, 0))
        probe = tmp_path / f"probe_{lang}.png"
        canvas.save(probe)
        assert _ink_ratio(probe) > 0.01, f"{lang} 直接绘制无墨迹"
        # 与空白对照：同尺寸几乎全白
        blank = Image.new("RGB", (800, 200), (255, 255, 255))
        blank_path = tmp_path / f"blank_{lang}.png"
        blank.save(blank_path)
        assert _ink_ratio(blank_path) < 0.001
        assert _ink_ratio(probe) > _ink_ratio(blank_path) * 10


@_NEEDS_FONTS
def test_cost_is_none_not_zero(client):
    """本地零成本：必须 None，写 0 会被下游当成"已核算成本"。"""
    res = client.repaint("mock://c1", "EN", **headline_kwargs())
    assert res.ok and res.data.cost_amount is None


@_NEEDS_FONTS
def test_all_text_lines_are_erased_not_just_the_tallest(tmp_path):
    """多行中文卖点必须**全部**擦掉，不能只擦最高的那条。

    这条测试补的是我自己犯过的错：docs/m1-demo-run.md 曾引用一个**根本不存在的**
    测试名当证据（连续两次编辑都没落盘）。现在它必须真的存在并真的跑过。
    夹具两行中文（源图 y≈40-96 / y≈112-168，归一到 600 后约 20-48 / 56-84）。
    去掉 band 检测的 gap_tol → 两行被切成两条带、只擦最高的那条，
    第二行中文留在图上 = "英文在上、中文还在下"的半翻译图。
    """
    c = PillowImageClient(output_dir=tmp_path / "o", target_size=600)
    res = c.repaint("mock://multiline-check", "EN", **headline_kwargs())
    assert res.ok, res.error
    band = res.data.notes["text_band_box"]
    assert band is not None, "两行中文带应被检出"
    assert band[3] >= 78, f"只覆盖第一行，第二行中文会残留: {band}"
    assert (band[3] - band[1]) > 45, f"带高过短，疑似未合并多行: {band}"

    # 直接查像素：第二行中文所在位置必须已被覆盖成浅色
    img = Image.open(res.data.output_path).convert("L")
    strip = img.crop((0, 52, img.width, 88))
    flat = list(strip.tobytes())
    dark = sum(1 for v in flat if v < 120) / len(flat)
    assert dark <= 0.30, f"第二行中文未擦净，深色像素占比 {dark:.3f}"


@_NEEDS_FONTS
def test_latency_is_measured(client):
    res = client.repaint("mock://l1", "EN", **headline_kwargs())
    assert res.ok and res.data.latency_ms >= 0


# ------------------------------------------------------------------ 两条"看图才发现"的回归

@_NEEDS_FONTS
def test_long_headline_wraps_instead_of_overflowing(client):
    """泰语长标题曾按固定字号直排，超出右边界被截成半句话。
    现在必须缩字号或折行，渲染出的每行都要装得下画布。"""
    long_headline = (
        "ชุดเด็กผู้หญิงเกาหลี ชุดฤดูใบไม้ผลิฤดูร้อน 2025 "
        "เสื้อกล้ามลูกไม้หมีน้อยพร้อมกางเกงยีนส์ขากว้างทรงยาว"
    )
    res = client.repaint("mock://wrap", "VI", overlay_headline=long_headline, overlay_sub="สแตนเลส 316 - 500 มล.")
    assert res.ok, res.error
    render = res.data.notes["text_render"]
    assert render, "未记录渲染几何，无法断言不溢出"
    assert len(render["lines"]) >= 1
    # 折行后单行字符数必须显著小于原文总长（说明真的拆开了，而不是整条硬画）
    assert max(render["lines"]) < len(long_headline), f"标题未折行: {render}"
    assert min(render["sizes"]) < 40, f"未做缩字号: {render}"


@_NEEDS_FONTS
def test_dark_product_region_is_not_erased(tmp_path):
    """深色商品本体（牛仔裤下摆）曾被误判成"中文文字带"整块擦掉。
    文字带判据必须要求浅底：深色区不得被擦。"""
    from PIL import Image as PImage

    w, h = 900, 1200
    img = PImage.new("RGB", (w, h), (252, 252, 252))
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.rectangle((150, 700, 750, 1180), fill=(60, 72, 96))  # 深色"牛仔裤"区，无浅底文字带
    p = tmp_path / "dark_pants.png"
    img.save(p)

    c = PillowImageClient(output_dir=tmp_path / "out", target_size=600)
    res = c.repaint(str(p), "EN", overlay_headline="Denim Set", overlay_sub="Soft fabric")
    assert res.ok, res.error
    assert res.data.notes["text_band_box"] is None, f"深色商品区被误判为文字带: {res.data.notes['text_band_box']}"
    # 留痕必须说清"没画字是因为没东西要替换"，不能让人误读成"已翻译的图"
    assert res.data.notes["copy_rendered"] is False
    assert res.data.notes["no_copy_reason"]


# ------------------------------------------------------------------ 失败语义

def test_empty_ref_is_validation(client):
    res = client.repaint("", "EN")
    assert not res.ok and res.error.kind == ErrorKind.VALIDATION


def test_missing_lang_is_validation(client):
    res = client.repaint("mock://x", "")
    assert not res.ok and res.error.kind == ErrorKind.VALIDATION


def test_missing_local_file_is_generation_error(client):
    res = client.repaint("C:\\definitely\\not\\here.png", "EN")
    assert not res.ok and res.error.code == "IMG_FILE_MISSING"
    assert res.error.kind == ErrorKind.GENERATION and res.data is None


def test_fail_switch_degrades_to_original_kept(client):
    broken = PillowImageClient(
        output_dir="artifacts/_unused", fail_for_test=AdapterError.generation("IMG_PILLOW_BOOM", "处理炸了")
    )
    res = broken.repaint("mock://z", "EN")
    assert not res.ok
    assert res.error.human_todo is True
    assert res.error.detail["fallback"] == "original_kept"
    assert res.error.detail["source_ref"] == "mock://z"


def test_unsupported_lang_font_is_reported(tmp_path):
    """没有该语种字体时不能抛异常，要返回 IMG_FONT_MISSING。"""
    c = PillowImageClient(output_dir=tmp_path / "f", lang_fonts={"XX": ("__no_such_font__.ttf",)})
    res = c.repaint("mock://f1", "XX", **headline_kwargs())
    assert not res.ok and res.error.code == "IMG_FONT_MISSING"
    assert res.error.kind == ErrorKind.GENERATION


# ------------------------------------------------------------------ 真实 1688 图

def _cdn_alive() -> bool:
    try:
        r = requests.head(REAL_IMG, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.real
@pytest.mark.skipif(not _cdn_alive(), reason="1688 CDN 不可达")
def test_real_1688_cdn_image_processes(tmp_path):
    """关键区分：1688 **详情页**被反爬拦，但**图片 CDN** 不拦。
    所以 M1-B 的图像腿可以是真的——源图直接下得来。"""
    c = PillowImageClient(output_dir=tmp_path / "real", target_size=800)
    res = c.repaint(REAL_IMG, "EN", overlay_headline="Toddler Girls 2-Piece Denim Set", overlay_sub="Bear lace trim vest + wide-leg pants")
    assert res.ok, res.error
    assert res.data.notes["provenance"] == "remote"
    assert Path(res.data.output_path).stat().st_size > 10_000, "真实图产物不该是空壳"
    img = Image.open(res.data.output_path)
    img.load()
    assert img.size == (800, 800)
