"""阶段 2：Demo 交付目录里必须同时有选品报告、三语文案和商品图文件。

mock 即可，不访问外网。负向用例确认降级引用不会被填成一张假图。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from shopkeeper.adapters.mock import MockImage
from shopkeeper.cli import main
from shopkeeper.demo_package import materialize_delivery_package
from shopkeeper.errors import AdapterError

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def _pixels(path: Path) -> str:
    img = Image.open(path)
    img.load()
    return hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()


def test_mock_demo_package_has_report_copy_and_images(tmp_path, monkeypatch):
    monkeypatch.setenv("LISTING_LANGS", "EN,TH,VI")
    out = tmp_path / "demo-out"
    code = main(["demo", "--mock", "--url", FIXTURE_URL, "--out", str(out)])
    assert code == 0

    packages = list((out / "packages").glob("listing_*"))
    assert len(packages) == 1
    pkg = packages[0]

    report = json.loads((pkg / "report.json").read_text(encoding="utf-8"))
    assert report["source_url"] == FIXTURE_URL
    assert report["report"]["price_gap"]["status"]
    assert report["report"]["competition"]["status"]
    assert report["report"]["compliance_risk"]["status"]
    assert (pkg / "report.json").stat().st_size > 0

    bodies = {}
    for lang in ("EN", "TH", "VI"):
        text = (pkg / "copy" / f"{lang}.txt").read_text(encoding="utf-8")
        assert text.strip()
        bodies[lang] = text
    assert "USB" in bodies["EN"]
    assert "พัด" in bodies["TH"]
    assert "Quạt" in bodies["VI"]

    listing = json.loads((pkg / "listing.json").read_text(encoding="utf-8"))
    assert listing["langs"] == ["EN", "TH", "VI"]
    assert listing["delivery"]["publish"] is False
    assert listing["delivery"]["wall_s"] < 180
    assert "published" not in listing

    hashes = set()
    for lang, assets in listing["images_by_lang"].items():
        assert assets, lang
        for asset in assets:
            rel = asset["output_path"]
            assert rel.startswith("images/")
            assert not rel.startswith(("http://", "https://"))
            path = pkg / rel
            assert path.is_file()
            assert path.stat().st_size > 0
            hashes.add(_pixels(path))
    assert len(hashes) == 3

    index = (pkg / "index.md").read_text(encoding="utf-8")
    assert "墙钟：" in index
    assert "卖家中心" in index
    assert "report.json" in index


def test_degraded_image_is_not_invented(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shopkeeper.cli._image_client",
        lambda _mock: MockImage(fail=AdapterError.generation("IMG_UPSTREAM_500", "重绘服务 500")),
    )
    monkeypatch.setenv("LISTING_LANGS", "EN,TH,VI")
    out = tmp_path / "degraded"
    code = main(["demo", "--mock", "--url", FIXTURE_URL, "--out", str(out)])
    assert code == 2
    pkg = next((out / "packages").glob("listing_*"))
    assert (pkg / "report.json").stat().st_size > 0
    assert (pkg / "copy" / "EN.txt").stat().st_size > 0
    assert list(pkg.rglob("*.png")) == []
    listing = json.loads((pkg / "listing.json").read_text(encoding="utf-8"))
    assert all(
        asset["package_status"] == "not_a_file"
        for assets in listing["images_by_lang"].values()
        for asset in assets
    )


def test_materialize_skips_missing_and_empty_files(tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    listing = {
        "listing_id": "neg",
        "langs": ["EN", "TH"],
        "versions": [
            {"lang": "EN", "title": "Fan", "bullets": ["a", "b", "c"], "description": "desc"},
            {"lang": "TH", "title": "", "bullets": [], "description": ""},
        ],
        "images_by_lang": {
            "EN": [
                {
                    "source_ref": "https://example.invalid/a.jpg",
                    "output_path": "https://example.invalid/a.jpg",
                    "mode": "original_kept",
                }
            ],
            "TH": [
                {
                    "source_ref": "mock://gone",
                    "output_path": str(empty),
                    "mode": "repainted",
                },
                {
                    "source_ref": "mock://missing",
                    "output_path": str(tmp_path / "no-such.png"),
                    "mode": "post_processed",
                },
            ],
        },
    }
    info = materialize_delivery_package(
        listing,
        report={"price_gap": {"status": "estimated"}},
        packages_dir=tmp_path / "packages",
        wall_s=1.2,
        source_url=FIXTURE_URL,
    )
    assert info["images_complete"] is False
    assert info["image_files"] == []
    pkg = info["directory"]
    assert not list((pkg / "images").rglob("*.png"))
    assert (pkg / "copy" / "EN.txt").stat().st_size > 0
    assert not (pkg / "copy" / "TH.txt").exists()
    stored = json.loads((pkg / "listing.json").read_text(encoding="utf-8"))
    assert stored["images_by_lang"]["EN"][0]["package_status"] == "not_a_file"
    assert stored["images_by_lang"]["TH"][0]["package_status"] == "empty"
    assert stored["images_by_lang"]["TH"][1]["package_status"] == "missing"
