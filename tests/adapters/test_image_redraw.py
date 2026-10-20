"""M2 真重绘：开关关→Pillow；开→mock redraw；水印 not_found 写 warnings 不进 needs_human。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from shopkeeper.adapters.image_redraw import (
    MockImageRedraw,
    RedrawAwareImageClient,
    StubGatewayImageRedraw,
    build_image_client,
)
from shopkeeper.adapters.pillow_image import PillowImageClient
from shopkeeper.adapters.mock import MockImage
from shopkeeper.config import Settings
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Shop
from shopkeeper.db.repo import upsert_product
from shopkeeper.demo_package import materialize_delivery_package
from shopkeeper.stylist import StylistPipeline
from shopkeeper.adapters.mock import MockLLM, MockProductSource


def _clean_png(tmp_path: Path, name: str = "src.png") -> str:
    p = tmp_path / name
    Image.new("RGB", (400, 400), (250, 250, 250)).save(p)
    return str(p)


def _badge_png(tmp_path: Path, name: str = "badge.png") -> str:
    """右下角红角标，供「重绘后仍检出」路径。"""
    p = tmp_path / name
    img = Image.new("RGB", (400, 400), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((320, 320, 395, 395), fill=(220, 40, 40))
    img.save(p)
    return str(p)


def test_redraw_disabled_uses_pillow(tmp_path: Path):
    pillow = PillowImageClient(output_dir=tmp_path / "pillow", target_size=400)
    redraw = MockImageRedraw(output_dir=tmp_path / "redraw")
    client = RedrawAwareImageClient(
        postprocess=pillow,
        redraw=redraw,
        redraw_enabled=False,
        watermark_inspector=pillow,
    )
    src = _clean_png(tmp_path)
    res = client.repaint(src, "VI", overlay_headline="Giữ nhiệt", overlay_sub="316")
    assert res.ok, res.error
    assert res.data.mode == "post_processed"
    assert res.data.notes.get("engine") == "pillow-postprocess-v1"
    assert not res.data.notes.get("redraw_attempted")
    # mock redraw 目录应为空（未调用）
    assert list((tmp_path / "redraw").glob("*.png")) == []


def test_redraw_enabled_uses_mock_redraw(tmp_path: Path):
    pillow = PillowImageClient(output_dir=tmp_path / "pillow", target_size=400)
    redraw = MockImageRedraw(output_dir=tmp_path / "redraw")
    client = RedrawAwareImageClient(
        postprocess=pillow,
        redraw=redraw,
        redraw_enabled=True,
        watermark_inspector=pillow,
    )
    src = _clean_png(tmp_path)
    res = client.repaint(src, "EN", overlay_headline="Stay cool", overlay_sub="Drawer bin")
    assert res.ok, res.error
    assert res.data.mode == "repainted"
    assert res.data.notes.get("engine") == "mock-image-redraw-v1"
    assert res.data.notes.get("redraw_attempted") is True
    assert res.data.notes.get("redraw_fallback") is False
    assert Path(res.data.output_path).is_file()
    assert Path(res.data.output_path).stat().st_size > 0
    # 确定性：同输入同路径
    again = client.repaint(src, "EN", overlay_headline="Stay cool", overlay_sub="Drawer bin")
    assert again.data.output_path == res.data.output_path


def test_watermark_not_found_warns_not_needs_human(tmp_path: Path):
    pillow = PillowImageClient(output_dir=tmp_path / "pillow", target_size=400)
    client = RedrawAwareImageClient(
        postprocess=pillow,
        redraw=MockImageRedraw(output_dir=tmp_path / "redraw"),
        redraw_enabled=True,
        watermark_inspector=pillow,
    )
    res = client.repaint(_clean_png(tmp_path), "VI", overlay_headline="Giữ nhiệt 24h")
    assert res.ok
    assert res.data.watermark_check == "not_found"
    assert res.data.needs_human is False
    assert res.data.notes["warnings"]
    assert any("检不出" in w for w in res.data.notes["warnings"])


def test_redraw_still_detects_corner_badge_as_needs_human(tmp_path: Path):
    """mock 重绘若产物仍带红角标 → failed + needs_human（与 not_found 区分）。"""
    pillow = PillowImageClient(output_dir=tmp_path / "pillow", target_size=400)

    class BadgeRedraw(MockImageRedraw):
        def redraw(self, image_ref, lang, *, overlay_headline=None, overlay_sub=None):
            # 先走父类写盘，再覆盖成带角标的图
            res = super().redraw(
                image_ref, lang, overlay_headline=overlay_headline, overlay_sub=overlay_sub
            )
            assert res.ok and res.data and res.data.output_path
            badge_src = _badge_png(tmp_path, "overlay_badge.png")
            Image.open(badge_src).save(res.data.output_path)
            return res

    client = RedrawAwareImageClient(
        postprocess=pillow,
        redraw=BadgeRedraw(output_dir=tmp_path / "redraw"),
        redraw_enabled=True,
        watermark_inspector=pillow,
    )
    res = client.repaint(_clean_png(tmp_path), "EN", overlay_headline="X")
    assert res.ok
    assert res.data.watermark_check == "failed"
    assert res.data.needs_human is True
    assert any("角标" in r for r in res.data.notes.get("needs_human_reasons", []))


def test_stub_gateway_does_not_outbound_and_falls_back_to_pillow(tmp_path: Path):
    pillow = PillowImageClient(output_dir=tmp_path / "pillow", target_size=400)
    stub = StubGatewayImageRedraw(enabled=True)
    client = RedrawAwareImageClient(
        postprocess=pillow,
        redraw=stub,
        redraw_enabled=True,
        watermark_inspector=pillow,
    )
    res = client.repaint(_clean_png(tmp_path), "EN", overlay_headline="Hello", overlay_sub="World")
    assert res.ok
    assert res.data.mode == "post_processed"
    assert res.data.notes.get("redraw_fallback") is True
    assert res.data.notes.get("redraw_error", {}).get("code") == "IMG_REDRAW_STUB"
    assert stub.outbound_calls == 0


def test_build_image_client_respects_switch(tmp_path: Path):
    off = build_image_client(
        image_channel="pillow",
        redraw_enabled=False,
        output_dir=tmp_path / "a",
    )
    assert isinstance(off, PillowImageClient)

    on = build_image_client(
        image_channel="pillow",
        redraw_enabled=True,
        redraw_adapter="mock",
        output_dir=tmp_path / "b",
    )
    assert isinstance(on, RedrawAwareImageClient)
    assert on.redraw_enabled is True
    assert isinstance(on.redraw, MockImageRedraw)

    mock_off = build_image_client(image_channel="mock", redraw_enabled=False, output_dir=tmp_path / "c")
    assert isinstance(mock_off, MockImage)


def test_stylist_optional_redraw_lands_in_delivery_package(tmp_path: Path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session = make_session(engine)
    session.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    session.commit()

    facts = MockProductSource().fetch("https://detail.1688.com/offer/600123456789.html").data
    # 用本地干净图替换远程图，避免网络
    local = _clean_png(tmp_path, "product.png")
    facts = facts.model_copy(update={"main_images": [local]})
    product, _ = upsert_product(
        session,
        {
            "source_platform": facts.source_platform,
            "source_item_id": facts.source_item_id,
            "source_url": facts.source_url,
            "title_raw": facts.title_raw,
            "reference_cost_price": facts.price_cents,
            "cost_currency": facts.currency,
            "moq": facts.moq,
            "images": facts.main_images,
        },
    )
    session.commit()

    pillow = PillowImageClient(output_dir=tmp_path / "imgs", target_size=400)
    image = RedrawAwareImageClient(
        postprocess=pillow,
        redraw=MockImageRedraw(output_dir=tmp_path / "imgs"),
        redraw_enabled=True,
        watermark_inspector=pillow,
    )
    pipe = StylistPipeline(
        session,
        MockLLM(),
        image,
        Settings(langs=("EN", "VI"), site="SEA"),
        shop_id="shop_1",
    )
    out = pipe.run(facts, product_db_id=product.id)
    assert out["status"] == "succeeded"
    listing = out["output"]
    # 至少一语种走了 redraw
    modes = [a["mode"] for assets in listing["images_by_lang"].values() for a in assets]
    assert "repainted" in modes
    pkg = materialize_delivery_package(
        listing,
        report={},
        packages_dir=tmp_path / "packages",
        product_id=product.id,
    )
    assert pkg["images_complete"] is True
    assert any("images/" in p.replace("\\", "/") for p in pkg["image_files"])
    session.close()
