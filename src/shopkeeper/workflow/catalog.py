"""工作台商品库（DESIGN §9 M2：商品库最小页 + 最小编辑）。

汇总 listing 草稿审批态与平台商品 ID；Demo 交付包 / 闸门上架可写入。
支持本地编辑 title / price_amount / currency / specs：不外呼平台；
编辑后 approval_status 回 pending，须重新审批才能 publish。
真写外呼仍由平台适配器与 TTS_WRITE_* 负责。

`import-demo` 的 packages_dir 必须落在工作目录下的 `artifacts/demo/` 内，防路径穿越。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from shopkeeper.workflow.store import WorkflowStore

DEMO_SANDBOX_REL = Path("artifacts") / "demo"
EDITABLE_FIELDS = frozenset({"title", "price_amount", "currency", "specs"})


class CatalogError(Exception):
    """商品库导入/路径/编辑错误（映射为 HTTP 400/404）。"""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def demo_sandbox_root(*, cwd: Path | None = None) -> Path:
    base = (cwd or Path.cwd()).resolve()
    return (base / DEMO_SANDBOX_REL).resolve()


def resolve_demo_packages_dir(
    packages_dir: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> Path:
    """解析并校验 packages_dir，拒绝逃出 artifacts/demo。"""
    base = (cwd or Path.cwd()).resolve()
    sandbox = demo_sandbox_root(cwd=base)
    raw = None if packages_dir is None or str(packages_dir).strip() == "" else str(packages_dir).strip()
    if raw is None:
        env = os.environ.get("WORKFLOW_DEMO_PACKAGES") or os.environ.get("DEMO_PACKAGES_DIR")
        candidate = Path(env) if env else sandbox / "packages"
    else:
        candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        resolved.relative_to(sandbox)
    except ValueError as exc:
        raise CatalogError(
            f"packages_dir 必须位于 {sandbox} 内（防路径穿越），收到：{resolved}"
        ) from exc
    return resolved


def default_demo_packages_root(*, cwd: Path | None = None) -> Path:
    return resolve_demo_packages_dir(None, cwd=cwd)


def upsert_from_draft(store: WorkflowStore, draft: dict[str, Any]) -> dict[str, Any]:
    """闸门上架或草稿状态变化后写入商品库一行。"""
    if draft.get("kind") != "listing":
        raise ValueError("只有 listing 草稿可写入商品库。")
    payload = dict(draft.get("payload") or {})
    title = str(draft.get("title") or payload.get("title_en") or draft.get("id") or "untitled")
    inp = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    return store.upsert_catalog_item(
        {
            "id": f"draft:{draft['id']}",
            "shop_id": draft.get("shop_id"),
            "source": "draft",
            "title": title,
            "approval_status": str(draft.get("status") or "pending"),
            "platform_product_id": draft.get("platform_product_id"),
            "draft_id": draft["id"],
            "listing_id": payload.get("listing_id"),
            "package_path": None,
            "payload": {
                "title_en": payload.get("title_en") or "",
                "body_th": draft.get("body_th") or "",
                "body_en": draft.get("body_en") or "",
                "input": inp,
                "publish": payload.get("publish") or {},
                "price_amount": payload.get("price_amount") or inp.get("price_amount") or "",
                "currency": payload.get("currency") or inp.get("currency") or "",
                "specs": payload.get("specs") or inp.get("specs") or "",
                "catalog_edit": payload.get("catalog_edit"),
            },
        }
    )


def import_demo_packages(
    store: WorkflowStore,
    packages_dir: Path | str | None = None,
    *,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """扫描 Demo 交付包 `listing_*/listing.json` 写入商品库。幂等按 listing_id。"""
    root = resolve_demo_packages_dir(packages_dir, cwd=cwd)
    if not root.exists():
        return {
            "imported": 0,
            "skipped": 0,
            "packages_dir": str(root),
            "items": [],
            "note": f"交付目录不存在：{root}。先跑 make demo（输出到 artifacts/demo）。",
        }
    imported = 0
    skipped = 0
    items: list[dict[str, Any]] = []
    for pkg in sorted(root.glob("listing_*")):
        listing_path = pkg / "listing.json"
        if not listing_path.is_file():
            skipped += 1
            continue
        try:
            listing = json.loads(listing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        listing_id = str(listing.get("listing_id") or pkg.name.replace("listing_", "", 1))
        versions = listing.get("versions") or []
        title = ""
        for ver in versions:
            if isinstance(ver, dict) and str(ver.get("lang") or "").upper() == "TH":
                title = str(ver.get("title") or "")
                break
        if not title and versions and isinstance(versions[0], dict):
            title = str(versions[0].get("title") or "")
        if not title:
            title = listing_id
        delivery = listing.get("delivery") or {}
        item = store.upsert_catalog_item(
            {
                "id": f"demo:{listing_id}",
                "source": "demo_package",
                "title": title,
                "approval_status": "demo",
                "platform_product_id": listing.get("platform_product_id")
                or delivery.get("platform_product_id"),
                "draft_id": None,
                "listing_id": listing_id,
                "package_path": str(pkg),
                "payload": {
                    "langs": listing.get("langs") or [],
                    "source_url": delivery.get("source_url"),
                    "product_id": delivery.get("product_id"),
                    "publish": delivery.get("publish"),
                    "note": delivery.get("note") or "Demo 交付包。纯生成，未上架。",
                },
            }
        )
        items.append(item)
        imported += 1
    return {
        "imported": imported,
        "skipped": skipped,
        "packages_dir": str(root),
        "items": items,
        "note": "已从 Demo 交付包写入本地商品库。编辑后置；真实 TikTok 写仍关闭。",
    }


def sync_listing_drafts(store: WorkflowStore) -> int:
    """把当前 listing 草稿同步进商品库，保证审批态可见。"""
    n = 0
    for draft in store.list_drafts():
        if draft.get("kind") != "listing":
            continue
        upsert_from_draft(store, draft)
        n += 1
    return n


def update_catalog_item(store: WorkflowStore, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """本地编辑 title / price_amount / currency / specs。

    不外呼平台、不改已发布平台商品。编辑后 approval_status → pending；
    有关联 listing 草稿时同步拉回 pending（保留历史 platform_product_id），须重新审批才能 publish。
    """
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise CatalogError(f"不可编辑字段：{', '.join(sorted(unknown))}")
    updates = {key: fields[key] for key in EDITABLE_FIELDS if key in fields}
    if not updates:
        raise CatalogError("至少提供 title、price_amount、currency、specs 之一。")

    item = store.get_catalog_item(item_id)
    if item is None:
        raise CatalogError(f"商品库条目不存在：{item_id}", not_found=True)

    title = str(item.get("title") or item_id)
    if "title" in updates:
        title = str(updates["title"] or "").strip()
        if not title:
            raise CatalogError("title 不能为空。")

    input_fields: dict[str, Any] = {}
    for key in ("price_amount", "currency", "specs"):
        if key not in updates:
            continue
        value = updates[key]
        input_fields[key] = "" if value is None else str(value)

    draft_id = item.get("draft_id")
    if draft_id:
        reset = store.reset_draft_after_catalog_edit(
            str(draft_id),
            title=title,
            input_fields=input_fields,
        )
        if reset is None:
            raise CatalogError(f"关联草稿不可编辑或不存在：{draft_id}")
        from shopkeeper.workflow.approvals import ensure_listing_approval

        ensure_listing_approval(store, reset)
        return upsert_from_draft(store, reset)

    payload = dict(item.get("payload") or {})
    inp = dict(payload.get("input") or {})
    for key, value in input_fields.items():
        payload[key] = value
        inp[key] = value
    payload["input"] = inp
    if "title" in updates:
        payload["title_en"] = title
    return store.upsert_catalog_item(
        {
            "id": item["id"],
            "source": item.get("source") or "unknown",
            "title": title,
            "approval_status": "pending",
            "platform_product_id": item.get("platform_product_id"),
            "draft_id": item.get("draft_id"),
            "listing_id": item.get("listing_id"),
            "package_path": item.get("package_path"),
            "payload": payload,
        }
    )


def list_catalog(
    store: WorkflowStore,
    *,
    sync_drafts: bool = True,
    shop_id: str | None = None,
) -> dict[str, Any]:
    if sync_drafts:
        sync_listing_drafts(store)
    items = store.list_catalog_items(shop_id=shop_id)
    return {
        "items": items,
        "count": len(items),
        "shop_id": shop_id,
        "note": (
            "本地商品库。含审批状态与平台商品 ID。"
            "可 PATCH/PUT 编辑 title/price/currency/specs；编辑后回 pending，须重新审批才能 publish。"
            "不触已发布平台商品。"
        ),
    }
