"""真重绘适配器（DESIGN §8 M2）+ 可选组合进 ImageClient。

- `ImageRedrawAdapter`：mock 确定性；真实网关默认 stub（不外呼）。
- `RedrawAwareImageClient`：`IMAGE_REDRAW_ENABLED=0` → Pillow 后处理；
  开启 → redraw 后再跑角标水印检测（检不出写 warnings，不计入 needs_human）。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

from shopkeeper.adapters.base import ImageClient, ImageRedrawAdapter
from shopkeeper.adapters.pillow_image import PillowImageClient
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import AdapterResult, ImageAsset


def image_redraw_enabled_from_env(raw: str | None = None) -> bool:
    text = (raw if raw is not None else os.getenv("IMAGE_REDRAW_ENABLED", "0")).strip().lower()
    return text in {"1", "true", "yes", "on"}


class MockImageRedraw(ImageRedrawAdapter):
    """确定性 mock 重绘：写本地 PNG，不访问网络。"""

    name = "mock_image_redraw"

    def __init__(
        self,
        *,
        output_dir: str | os.PathLike[str] = "artifacts/demo/images",
        delay_ms: int = 0,
        fail: AdapterError | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._delay_ms = int(delay_ms)
        self._fail = fail

    def redraw(
        self,
        image_ref: str,
        lang: str,
        *,
        overlay_headline: str | None = None,
        overlay_sub: str | None = None,
    ) -> AdapterResult[ImageAsset]:
        started = time.monotonic()
        if self._fail is not None:
            return AdapterResult.failure(self._fail, self.name, self._delay_ms)
        if not image_ref:
            return AdapterResult.failure(
                AdapterError.validation("IMG_NO_REF", "缺少源图引用"),
                self.name,
            )
        lang_u = (lang or "").upper()
        if not lang_u:
            return AdapterResult.failure(
                AdapterError.validation("IMG_NO_LANG", "缺少目标语种"),
                self.name,
            )
        digest = hashlib.sha256(f"redraw|{image_ref}|{lang_u}".encode("utf-8")).hexdigest()[:16]
        path = self.output_dir / f"{digest}_{lang_u}_redraw.png"
        try:
            _write_redraw_png(
                path,
                lang=lang_u,
                headline=overlay_headline or "",
                sub=overlay_sub or "",
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_PILLOW_BOOM",
                    f"mock 重绘写盘失败: {type(exc).__name__}",
                ),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        latency = max(self._delay_ms, int((time.monotonic() - started) * 1000))
        asset = ImageAsset(
            source_ref=image_ref,
            output_path=str(path),
            mode="repainted",
            needs_human=False,
            watermark_check=None,  # 组合层补检
            latency_ms=latency,
            cost_amount=0.0,
            notes={
                "engine": "mock-image-redraw-v1",
                "pipeline": "mock-redraw",
                "ai_generated": True,
                "copy": {"headline": overlay_headline or "", "sub": overlay_sub or ""},
                "copy_rendered": bool(overlay_headline),
            },
        )
        return AdapterResult.success(asset, self.name, latency)


class StubGatewayImageRedraw(ImageRedrawAdapter):
    """真实重绘 stub：默认不外呼；即使 IMAGE_REDRAW_ENABLED=1 也不假装通了图生图。

    本机网关不支持 edits（spike/image.md A5a/A5b）。真 img2img 接通前保持失败结果，
    由 RedrawAwareImageClient 降级回 Pillow。
    """

    name = "stub_gateway_image_redraw"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.base_url = base_url or os.getenv("SHOPKEEPER_IMAGE_BASE_URL", "http://127.0.0.1:3425/v1")
        self.model = model or os.getenv("SHOPKEEPER_IMAGE_MODEL", "workbuddy/hunyuan-image-v3.0-art")
        self.enabled = image_redraw_enabled_from_env() if enabled is None else bool(enabled)
        self.outbound_calls = 0

    def redraw(
        self,
        image_ref: str,
        lang: str,
        *,
        overlay_headline: str | None = None,
        overlay_sub: str | None = None,
    ) -> AdapterResult[ImageAsset]:
        if not image_ref:
            return AdapterResult.failure(
                AdapterError.validation("IMG_NO_REF", "缺少源图引用"),
                self.name,
            )
        if not self.enabled:
            return AdapterResult.failure(
                AdapterError(
                    code="IMG_REDRAW_DISABLED",
                    kind=ErrorKind.VALIDATION,
                    message="IMAGE_REDRAW_ENABLED=0：真重绘关闭，不外呼。",
                    retryable=False,
                    human_todo=False,
                    detail={"fallback": "pillow_postprocess", "image_ref": image_ref},
                ),
                self.name,
            )
        # 显式不外呼：计数只增 stub 意图，HTTP 次数恒为 0
        self.outbound_calls += 0
        return AdapterResult.failure(
            AdapterError(
                code="IMG_REDRAW_STUB",
                kind=ErrorKind.GENERATION,
                message=(
                    "真重绘 stub：本机网关无图生图/edits（spike/image.md）；"
                    "不外呼、不编造成功。降级 Pillow 后处理。"
                ),
                retryable=False,
                human_todo=True,
                detail={
                    "fallback": "pillow_postprocess",
                    "base_url": self.base_url,
                    "model": self.model,
                    "lang": lang,
                    "outbound": False,
                },
            ),
            self.name,
        )


class RedrawAwareImageClient(ImageClient):
    """Pillow 主链路 + 可选真重绘。listing 管线注入本客户端即可切换。"""

    name = "redraw_aware_image"

    def __init__(
        self,
        *,
        postprocess: ImageClient | None = None,
        redraw: ImageRedrawAdapter | None = None,
        redraw_enabled: bool = False,
        watermark_inspector: PillowImageClient | None = None,
    ) -> None:
        self.postprocess = postprocess or PillowImageClient()
        self.redraw = redraw
        self.redraw_enabled = bool(redraw_enabled)
        self._wm = watermark_inspector or (
            self.postprocess if isinstance(self.postprocess, PillowImageClient) else PillowImageClient()
        )

    def repaint(
        self,
        image_ref: str,
        lang: str,
        *,
        watermark_check: bool = True,
        overlay_headline: str | None = None,
        overlay_sub: str | None = None,
    ) -> AdapterResult[ImageAsset]:
        if not self.redraw_enabled or self.redraw is None:
            return self.postprocess.repaint(
                image_ref,
                lang,
                watermark_check=watermark_check,
                overlay_headline=overlay_headline,
                overlay_sub=overlay_sub,
            )

        redraw_res = self.redraw.redraw(
            image_ref,
            lang,
            overlay_headline=overlay_headline,
            overlay_sub=overlay_sub,
        )
        if not redraw_res.ok or redraw_res.data is None:
            # 真重绘失败/stub → 降级 Pillow，不谎报重绘成功
            pillow_res = self.postprocess.repaint(
                image_ref,
                lang,
                watermark_check=watermark_check,
                overlay_headline=overlay_headline,
                overlay_sub=overlay_sub,
            )
            if pillow_res.ok and pillow_res.data is not None:
                notes = dict(pillow_res.data.notes or {})
                notes["redraw_attempted"] = True
                notes["redraw_fallback"] = True
                err = redraw_res.error
                notes["redraw_error"] = (
                    {"code": err.code, "message": err.message} if err else {"code": "IMG_NO_RESULT"}
                )
                pillow_res.data.notes = notes
            return pillow_res

        asset = redraw_res.data
        if watermark_check:
            wm = self._wm.evaluate_corner_watermark(asset.output_path or "")
            asset.watermark_check = wm["watermark_check"]
            notes = dict(asset.notes or {})
            warnings = list(notes.get("warnings") or [])
            warnings.extend(wm.get("warnings") or [])
            notes["warnings"] = warnings
            notes["watermark_box"] = wm.get("watermark_box")
            notes["residue"] = wm.get("residue")
            notes["redraw_attempted"] = True
            notes["redraw_fallback"] = False
            reasons = list(notes.get("needs_human_reasons") or [])
            if wm.get("needs_human_from_watermark"):
                asset.needs_human = True
                reasons.extend(wm.get("needs_human_reasons") or ["水印检测未过"])
            # not_found → 只留 warnings，不把 needs_human 翻成 True
            notes["needs_human_reasons"] = reasons
            asset.notes = notes
        else:
            asset.watermark_check = "not_implemented"
            notes = dict(asset.notes or {})
            notes["redraw_attempted"] = True
            asset.notes = notes
        return AdapterResult.success(asset, self.name, asset.latency_ms or redraw_res.duration_ms or 0)


def build_image_client(
    *,
    image_channel: str = "mock",
    redraw_enabled: bool | None = None,
    redraw_adapter: str | None = None,
    output_dir: str | os.PathLike[str] = "artifacts/demo/images",
) -> ImageClient:
    """按配置装配图像客户端。`IMAGE_CHANNEL=mock` 且重绘关 → 纯 MockImage（离线最快）。"""
    from shopkeeper.adapters.mock import MockImage

    enabled = image_redraw_enabled_from_env() if redraw_enabled is None else bool(redraw_enabled)
    adapter_name = (
        redraw_adapter
        if redraw_adapter is not None
        else os.getenv("IMAGE_REDRAW_ADAPTER", "mock").strip().lower()
    )
    channel = (image_channel or "mock").strip().lower()

    if channel == "mock" and not enabled:
        return MockImage()

    pillow = PillowImageClient(output_dir=output_dir)
    if not enabled:
        return pillow

    if adapter_name in {"stub", "gateway", "stub_gateway"}:
        redraw: ImageRedrawAdapter = StubGatewayImageRedraw(enabled=True)
    else:
        redraw = MockImageRedraw(output_dir=output_dir)

    postprocess: ImageClient = MockImage() if channel == "mock" else pillow
    inspector = pillow
    return RedrawAwareImageClient(
        postprocess=postprocess,
        redraw=redraw,
        redraw_enabled=True,
        watermark_inspector=inspector,
    )


def _write_redraw_png(path: Path, *, lang: str, headline: str, sub: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    # 干净浅底 + 色块标记「已重绘」，不含红橙角标 → 水印检测走 not_found/warnings
    seed = int(hashlib.sha256(f"{path.name}|{lang}".encode()).hexdigest()[:8], 16)
    bg = (245, 248, 252)
    accent = ((seed >> 16) & 80) + 40, ((seed >> 8) & 80) + 100, (seed & 80) + 140
    img = Image.new("RGB", (640, 640), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 40, 600, 200), fill=accent)
    font = ImageFont.load_default()
    draw.text((56, 60), f"REDRAW {lang}", fill=(255, 255, 255), font=font)
    if headline:
        draw.text((56, 100), headline[:48], fill=(255, 255, 255), font=font)
    if sub:
        draw.text((56, 130), sub[:48], fill=(230, 240, 255), font=font)
    draw.text((56, 560), "mock-image-redraw-v1", fill=(120, 120, 130), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
