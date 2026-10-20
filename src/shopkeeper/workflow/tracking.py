"""物流回填（DESIGN §9 M3 / §7.2，P2-03）。

代发执行结果中的承运商 + 物流单号 → 本地 `shipments`；
`POST …/tracking/draft` 建八态审批 `action=update_tracking`；
批准后 `…/tracking/push` → 承运商格式门 → 可选只读查询 → `PlatformAdapter.update_tracking`。
查询默认关。未审批 → 409；格式不过或查询未确认不回传。
`update_tracking` 真写仍受 `TTS_WRITE_*`，默认关。发货、退款、付款仍禁止。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import httpx

from shopkeeper.adapters.base import PlatformAdapter
from shopkeeper.domain.approval_state import ApprovalEvent, ApprovalStatus
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.side_effect import make_idempotency_key, run_side_effect
from shopkeeper.workflow.approvals import (
    ApprovalError,
    apply_approval_event,
    claim_for_execute,
    evaluate_gate_policy,
    payload_hash,
)
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.store import WorkflowStore

TRACKING_ACTION = "update_tracking"
EXECUTE_ACTION = "update_tracking"
DECIDE_TTL = timedelta(days=7)

# 格式门。正则命中只说明样子对，不说明承运方库里有这票。
# THAIPOST 另过 UPU S10 校验位（S10-12 §5.4：权重 8 6 4 2 3 5 9 7）。
# 其余承运商没有可核验的公开校验位，只做正则；真确认靠可选只读查询。
_CARRIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "JNT": re.compile(r"^JT\d{10,15}$"),
    "FLASH": re.compile(r"^TH\d{10,15}$"),
    "SPX": re.compile(r"^SPX[A-Z0-9]{10,20}$"),
    "KERRY": re.compile(r"^KY\d{10,15}$"),
    "THAIPOST": re.compile(r"^[A-Z]{2}\d{9}TH$"),
}
_CARRIER_ALIASES = {
    "JT": "JNT",
    "JTEXPRESS": "JNT",
    "JNTEXPRESS": "JNT",
    "FLASHEXPRESS": "FLASH",
    "KERRYEXPRESS": "KERRY",
    "THPOST": "THAIPOST",
    "THAILANDPOST": "THAIPOST",
    "SHOPEEEXPRESS": "SPX",
    "SPXEXPRESS": "SPX",
}
_S10_WEIGHTS = (8, 6, 4, 2, 3, 5, 9, 7)
_LOOKUP_ON = frozenset({"1", "true", "yes", "on"})
_MAX_LOOKUP_BODY = 65_536

ALLOWED_CARRIERS = frozenset(_CARRIER_PATTERNS)


def normalize_carrier(carrier: str | None) -> str:
    return str(carrier or "").strip().upper().replace(" ", "").replace("&", "")


def canonical_carrier(carrier: str | None) -> str:
    code = normalize_carrier(carrier)
    return _CARRIER_ALIASES.get(code, code)


def s10_check_digit(serial8: str) -> str:
    """UPU S10 加权模 11。`serial8` 为 8 位流水号。"""
    if len(serial8) != 8 or not serial8.isdigit():
        raise ValueError("S10 流水号须为 8 位数字")
    total = sum(int(digit) * weight for digit, weight in zip(serial8, _S10_WEIGHTS, strict=True))
    value = 11 - (total % 11)
    if value == 10:
        return "0"
    if value == 11:
        return "5"
    return str(value)


def thaipost_s10_ok(number: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}\d{9}TH", number):
        return False
    return number[10] == s10_check_digit(number[2:10])


def _lookup_flag(env: Mapping[str, str]) -> bool:
    return str(env.get("TRACKING_LOOKUP_ENABLED", "0") or "0").strip().lower() in _LOOKUP_ON


def _lookup_timeout_s(env: Mapping[str, str]) -> float:
    raw = str(env.get("TRACKING_LOOKUP_TIMEOUT_S", "5") or "5").strip()
    try:
        value = float(raw)
    except ValueError:
        return 5.0
    if value < 0.5:
        return 0.5
    if value > 15.0:
        return 15.0
    return value


def lookup_url_problem(template: str) -> str:
    """空串表示 URL 可用。不回显 URL，避免把写进地址的令牌带进错误信息。"""
    raw = str(template or "").strip()
    if not raw:
        return "未配置 TRACKING_LOOKUP_URL。"
    probe = raw
    if "{carrier}" not in probe and "{tracking_no}" not in probe:
        sep = "&" if "?" in probe else "?"
        probe = f"{probe}{sep}carrier=JNT&tracking_no=JT000000000000"
    else:
        probe = probe.replace("{carrier}", "JNT").replace("{tracking_no}", "JT000000000000")
    parsed = urlparse(probe)
    if parsed.scheme not in {"http", "https"}:
        return "TRACKING_LOOKUP_URL 只允许 http 或 https。"
    if not parsed.hostname:
        return "TRACKING_LOOKUP_URL 缺少主机名。"
    if parsed.username or parsed.password:
        return "不要把令牌写进 URL。改用 TRACKING_LOOKUP_BEARER。"
    return ""


def build_lookup_url(template: str, carrier: str, tracking_no: str) -> str:
    raw = str(template or "").strip()
    if "{carrier}" not in raw and "{tracking_no}" not in raw:
        sep = "&" if "?" in raw else "?"
        raw = f"{raw}{sep}carrier={{carrier}}&tracking_no={{tracking_no}}"
    return raw.replace("{carrier}", quote(carrier, safe="")).replace(
        "{tracking_no}", quote(tracking_no, safe="")
    )


def describe_lookup(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """设置页只读投影。调用方必须打码 `url` / `bearer`，不得原样展示。"""
    source = os.environ if env is None else env
    return {
        "enabled": _lookup_flag(source),
        "url": str(source.get("TRACKING_LOOKUP_URL", "") or "").strip(),
        "bearer": str(source.get("TRACKING_LOOKUP_BEARER", "") or "").strip(),
    }


class TrackingQuery(Protocol):
    def query(self, carrier: str, tracking_no: str) -> dict[str, Any]: ...


class SkippedTrackingQuery:
    """查询开关关闭。不创建 HTTP 客户端。"""

    def query(self, carrier: str, tracking_no: str) -> dict[str, Any]:
        return {
            "ok": True,
            "queried": False,
            "lookup_status": "skipped",
            "lookup_confirmed": False,
        }


class UnconfiguredTrackingQuery:
    def __init__(self, message: str) -> None:
        self._message = message

    def query(self, carrier: str, tracking_no: str) -> dict[str, Any]:
        return {
            "ok": False,
            "queried": False,
            "lookup_status": "unconfigured",
            "lookup_confirmed": False,
            "code": "TRACKING_LOOKUP_UNCONFIGURED",
            "message": f"{self._message}不外呼、不回传。",
        }


def interpret_lookup_payload(
    payload: Any,
    *,
    carrier: str,
    tracking_no: str,
) -> dict[str, Any]:
    """只认布尔 `found`。缺字段、字符串 `"true"` 都不算确认。"""
    failed = {
        "ok": False,
        "queried": True,
        "lookup_status": "failed",
        "lookup_confirmed": False,
        "code": "TRACKING_LOOKUP_FAILED",
    }
    if not isinstance(payload, dict):
        return {**failed, "message": "承运方查询响应不是 JSON 对象。不回传。"}
    if not isinstance(payload.get("found"), bool):
        return {
            **failed,
            "message": "承运方查询响应缺少布尔字段 found。不把缺字段当成已确认。不回传。",
        }
    if payload["found"] is False:
        return {
            "ok": False,
            "queried": True,
            "lookup_status": "not_found",
            "lookup_confirmed": False,
            "code": "TRACKING_NOT_FOUND",
            "message": f"承运方查询未找到单号 {tracking_no}。不回传。",
        }
    if "carrier" in payload:
        remote_carrier = str(payload.get("carrier") or "").strip()
        if not remote_carrier:
            return {**failed, "message": "承运方查询的 carrier 为空。不回传。"}
        if canonical_carrier(remote_carrier) != carrier:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "mismatch",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_MISMATCH",
                "message": "承运方查询返回的承运商与所问不一致。不回传。",
            }
    if "tracking_no" in payload:
        remote_no = str(payload.get("tracking_no") or "").strip().upper()
        if not remote_no:
            return {**failed, "message": "承运方查询的 tracking_no 为空。不回传。"}
        if remote_no != tracking_no:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "mismatch",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_MISMATCH",
                "message": "承运方查询返回的单号与所问不一致。不回传。",
            }
    return {
        "ok": True,
        "queried": True,
        "lookup_status": "confirmed",
        "lookup_confirmed": True,
    }


class HttpTrackingQuery:
    """一次只读 GET。不跟随重定向，不发送 POST。"""

    def __init__(
        self,
        url_template: str,
        *,
        bearer: str = "",
        timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url_template = url_template
        self._bearer = bearer
        self._timeout_s = timeout_s
        self._transport = transport

    def query(self, carrier: str, tracking_no: str) -> dict[str, Any]:
        problem = lookup_url_problem(self._url_template)
        if problem:
            return UnconfiguredTrackingQuery(problem).query(carrier, tracking_no)
        url = build_lookup_url(self._url_template, carrier, tracking_no)
        headers = {"Accept": "application/json"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_s,
                follow_redirects=False,
            ) as client:
                response = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "failed",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_FAILED",
                "message": "承运方查询超时。不把超时当成已确认。不回传。",
            }
        except httpx.HTTPError:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "failed",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_FAILED",
                "message": "承运方查询传输失败。不回传。",
            }
        if response.status_code != 200:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "failed",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_FAILED",
                "message": (
                    f"承运方查询返回 HTTP {response.status_code}。"
                    "非 200 不表示已确认。不回传。"
                ),
            }
        if len(response.content) > _MAX_LOOKUP_BODY:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "failed",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_FAILED",
                "message": "承运方查询响应过大。不回传。",
            }
        try:
            payload = response.json()
        except ValueError:
            return {
                "ok": False,
                "queried": True,
                "lookup_status": "failed",
                "lookup_confirmed": False,
                "code": "TRACKING_LOOKUP_FAILED",
                "message": "承运方查询响应不是 JSON。不回传。",
            }
        return interpret_lookup_payload(payload, carrier=carrier, tracking_no=tracking_no)


def lookup_from_env(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> TrackingQuery:
    source: Mapping[str, str] = os.environ if env is None else env
    if not _lookup_flag(source):
        return SkippedTrackingQuery()
    url = str(source.get("TRACKING_LOOKUP_URL", "") or "").strip()
    problem = lookup_url_problem(url)
    if problem:
        return UnconfiguredTrackingQuery(problem)
    return HttpTrackingQuery(
        url,
        bearer=str(source.get("TRACKING_LOOKUP_BEARER", "") or "").strip(),
        timeout_s=_lookup_timeout_s(source),
        transport=transport,
    )


def _format_failure(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "message": message,
        "format_ok": False,
        "lookup_status": "not_queried",
        "lookup_confirmed": False,
        "queried": False,
    }


def _public_validation(checked: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "format_ok": bool(checked.get("format_ok")),
        "lookup_status": str(checked.get("lookup_status") or "skipped"),
        "lookup_confirmed": bool(checked.get("lookup_confirmed")),
        "queried": bool(checked.get("queried")),
    }


def _success_note(adapter_data: Mapping[str, Any], validation: Mapping[str, Any]) -> str:
    if validation.get("lookup_confirmed"):
        head = "承运方查询已确认该单号。"
    else:
        head = "单号格式已通过。未查询承运方，不能把格式通过当成承运方已确认。"
    body = str(adapter_data.get("note") or "物流已写入本地 shipments。")
    return (
        f"{head}{body}发货、退款、付款仍关闭。"
        "平台回填真写仍受 TTS_WRITE_*，默认关。"
    )


def validate_tracking(
    carrier: str | None,
    tracking_no: str | None,
    *,
    lookup: TrackingQuery | None = None,
) -> dict[str, Any]:
    """格式门必过。`lookup` 缺省为不查询；push 会按环境变量传入查询器。

    格式不过时不调用 `lookup`。查询关闭时 `lookup_confirmed` 为 false。
    """
    code = canonical_carrier(carrier)
    number = str(tracking_no or "").strip().upper()
    if not code:
        return _format_failure("TRACKING_REQUIRED", "缺少承运商 carrier。不回传。")
    if not number:
        return _format_failure("TRACKING_REQUIRED", "缺少物流单号 tracking_no。不回传。")
    pattern = _CARRIER_PATTERNS.get(code)
    if pattern is None:
        return _format_failure(
            "TRACKING_INVALID",
            f"承运商 {code} 不在白名单 {sorted(ALLOWED_CARRIERS)}。不回传。",
        )
    if not pattern.match(number):
        return _format_failure(
            "TRACKING_INVALID",
            f"单号 {number} 不符合承运商 {code} 的格式。不回传。",
        )
    if code == "THAIPOST" and not thaipost_s10_ok(number):
        return _format_failure(
            "TRACKING_INVALID",
            f"单号 {number} 未通过 Thailand Post（UPU S10）校验位。不回传。",
        )
    query = lookup if lookup is not None else SkippedTrackingQuery()
    outcome = query.query(code, number)
    if not outcome.get("ok"):
        return {
            "ok": False,
            "code": str(outcome.get("code") or "TRACKING_LOOKUP_FAILED"),
            "message": str(outcome.get("message") or "承运方查询未确认。不回传。"),
            "carrier": code,
            "tracking_no": number,
            "format_ok": True,
            "lookup_status": str(outcome.get("lookup_status") or "failed"),
            "lookup_confirmed": False,
            "queried": bool(outcome.get("queried")),
        }
    return {
        "ok": True,
        "carrier": code,
        "tracking_no": number,
        "format_ok": True,
        "lookup_status": str(outcome.get("lookup_status") or "skipped"),
        "lookup_confirmed": bool(outcome.get("lookup_confirmed")),
        "queried": bool(outcome.get("queried")),
    }


def create_tracking_draft(
    store: WorkflowStore,
    order_id: str,
    *,
    carrier: str | None = None,
    tracking_no: str | None = None,
    shipping_provider_id: str | None = None,
) -> dict[str, Any]:
    """从代发执行结果（或请求体覆盖）生成物流回填草稿 + update_tracking 审批单。"""
    oid = str(order_id or "").strip()
    if not oid:
        raise DraftError("order_id 不能为空。")
    order = store.get_synced_order(oid)
    if order is None:
        raise DraftError("订单不存在。请先同步订单。")

    existing = store.get_tracking_draft(oid)
    dropship = store.get_dropship_draft(oid)
    if dropship is None or dropship.get("status") != "executed":
        raise DraftError("代发尚未执行。请先完成 dropship/execute。")

    result = dict(dropship.get("result") or {})
    current_payload = dict((existing or {}).get("payload") or {})
    draft_payload = _tracking_snapshot({
        "order_id": oid,
        "carrier": carrier if carrier is not None else current_payload.get("carrier", result.get("carrier")),
        "tracking_no": tracking_no if tracking_no is not None else current_payload.get("tracking_no", result.get("tracking_no")),
        "external_po_id": dropship.get("external_po_id") or result.get("external_po_id"),
        "shipping_provider_id": (
            shipping_provider_id
            if shipping_provider_id is not None
            else current_payload.get("shipping_provider_id")
            or result.get("shipping_provider_id")
            or os.getenv("TIKTOK_SHIPPING_PROVIDER_ID", "")
        ),
    })
    if not draft_payload["carrier"] or not draft_payload["tracking_no"]:
        raise DraftError("缺少承运商或物流单号。代发结果或请求体须提供 carrier / tracking_no。")

    revision = _ensure_tracking_approval(store, oid, draft_payload, expected=existing)
    approval = revision["approval"]
    saved = revision["draft"]
    return {
        "draft": saved,
        "approval": approval,
        "shipment": store.get_shipment_for_order(oid) if saved["status"] == "executed" else None,
        "idempotent_replay": bool(revision["reused"]),
        "note": "物流快照相同，返回既有审批。" if revision["reused"] else "已生成物流回填草稿并进入审批。默认不外呼；承运方查询默认关，只在 push 且显式开启时发只读 GET。",
    }


def push_tracking(
    store: WorkflowStore,
    platform: PlatformAdapter,
    order_id: str,
    *,
    lookup: TrackingQuery | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """审批通过后格式校验，可选只读查询，再写本地 shipments 并调 update_tracking。

    未审批 → 409。查询失败不 claim、不回传。真写仍由适配器上的 TTS_WRITE_* 决定。
    """
    oid = str(order_id or "").strip()
    if not oid:
        raise DraftError("order_id 不能为空。")
    draft = store.get_tracking_draft(oid)
    if draft is None:
        raise DraftError("物流回填草稿不存在。请先 POST …/tracking/draft。")
    approval = store.get_approval(str(draft["approval_id"])) if draft.get("approval_id") else None
    if approval is None:
        raise DraftError("未审批，不能回填物流。请先生成草稿并人工通过。")
    payload = _approved_tracking_snapshot(approval)
    if (payload is None or approval["action_type"] != TRACKING_ACTION or approval["entity_type"] != "order"
            or approval["entity_id"] != oid or payload["order_id"] != oid
            or _tracking_snapshot(draft.get("payload") or {}) != payload):
        raise DraftError("物流草稿与批准快照不一致，请重新生成草稿并审批。")
    if approval["status"] == ApprovalStatus.EXECUTED.value:
        return {
            "draft": draft, "approval": approval,
            "shipment": store.get_shipment_for_order(oid),
            "adapter": draft.get("result") or {}, "idempotent_replay": True,
            "note": "已回填过物流，返回本地结果。",
        }
    if approval["status"] == ApprovalStatus.EXECUTING.value or draft.get("status") == "executing":
        raise DraftError("物流回填正在执行，请稍后刷新；不会重复回传。")
    if approval["status"] == ApprovalStatus.PENDING.value:
        raise DraftError("未审批，不能回填物流。须先人工通过，且没有跳过本次。")
    if approval["status"] == ApprovalStatus.REJECTED.value:
        raise DraftError("已拒绝，不能回填物流。没有跳过本次。")
    if approval["status"] == ApprovalStatus.EXPIRED.value:
        raise DraftError("审批已过期，不能回填物流。请重新生成草稿并再审。")
    if approval["status"] not in {
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.EXECUTING.value,
    }:
        raise DraftError(f"当前审批状态 {approval['status']} 不能回填物流。")

    active = lookup if lookup is not None else lookup_from_env(environ)
    checked = validate_tracking(
        payload.get("carrier"),
        payload.get("tracking_no"),
        lookup=active,
    )
    if not checked.get("ok"):
        code = str(checked.get("code") or "TRACKING_INVALID")
        message = str(checked.get("message") or "物流单号不合法。不回传。")
        saved = store.update_tracking_result(
            oid, approval_id=approval["id"], payload=payload, status="validation_failed",
            result={"code": code, "message": message}, approved_only=True,
        )
        if saved is None:
            raise DraftError("物流快照或审批状态已变，请刷新后重试。")
        raise DraftError(f"{code} {message}")

    carrier = str(checked["carrier"])
    tracking_no = str(checked["tracking_no"])
    payload["carrier"] = carrier
    payload["tracking_no"] = tracking_no

    from shopkeeper.workflow.shops import resolve_platform

    order = store.get_synced_order(oid)
    if order is None:
        raise DraftError("订单不存在，不能选择物流回填店铺。")
    platform = resolve_platform(platform, order.get("shop_id"))
    try:
        claimed = claim_for_execute(store, approval["id"])
    except ApprovalError as exc:
        text = str(exc)
        if "未审批" in text or "过期" in text:
            raise DraftError(
                text.replace("不能上架", "不能回填物流").replace(
                    "请重新生成并再审", "请重新生成草稿并再审"
                )
            ) from exc
        raise DraftError(str(exc)) from exc
    frozen = _approved_tracking_snapshot(claimed)
    if claimed["status"] != ApprovalStatus.EXECUTING.value or frozen != payload:
        # claim 已把审批推到 executing；签名对不上时必须放回失败态，
        # 否则这张单会卡在 executing 直到 reaper，且草稿无法继续。
        if claimed.get("status") == ApprovalStatus.EXECUTING.value:
            try:
                apply_approval_event(
                    store,
                    claimed["id"],
                    ApprovalEvent.EXECUTE_FAIL,
                    execution_error="TRACKING_SNAPSHOT_MISMATCH",
                )
            except ApprovalError:
                pass
        raise DraftError("物流批准快照已变，未回传；请核对审批状态。")
    payload = frozen

    adapter_name = getattr(platform, "name", "platform")
    idem_key = make_idempotency_key(
        adapter_name, "update_tracking", oid, tracking_no, str(claimed["id"])
    )

    def _push() -> AdapterResult[dict[str, Any]]:
        return platform.update_tracking(
            oid,
            tracking_no,
            carrier=payload.get("carrier"),
            shipping_provider_id=payload.get("shipping_provider_id") or None,
        )

    outcome = run_side_effect(
        store,
        adapter=adapter_name,
        operation="update_tracking",
        idempotency_key=idem_key,
        request=payload,
        fn=_push,
        draft_id=None,
        action=EXECUTE_ACTION,
    )
    result = outcome.result
    write = outcome.write
    if result is None or not result.ok or result.data is None:
        err = (
            result.error
            if result is not None and result.error is not None
            else AdapterError.execution("TRACKING_PUSH_FAILED", "物流回填失败")
        )
        if str(err.code) == "EXT_IDEMPOTENCY_CONFLICT":
            # 同幂等键仍 pending：外部结果未知。不能标成失败诱使重试，
            # 也不能标成成功；保留 executing，交给 reaper + 人工核对。
            raise DraftError(
                "物流回填结果未知：同一幂等键仍在执行。"
                "已保留执行中状态，请人工核对后再处理；不会自动重试。"
            )
        # 无论是本次执行失败，还是同一审批重审后回放到的历史失败，
        # 都必须把审批与草稿落到 execute_failed，不能停在 executing。
        try:
            apply_approval_event(
                store,
                claimed["id"],
                ApprovalEvent.EXECUTE_FAIL,
                execution_error=f"{err.code} {err.message}",
            )
        except ApprovalError as exc:
            raise DraftError(
                f"物流回填失败，但审批状态已变（{exc}）：{err.code} {err.message}"
            ) from exc
        saved = store.update_tracking_result(
            oid, approval_id=claimed["id"], payload=payload,
            status="execute_failed", result=err.model_dump(),
        )
        if saved is None:
            raise DraftError(
                f"物流回填失败，且草稿身份已变：{err.code} {err.message}。请刷新核对。"
            )
        raise DraftError(f"物流回填未执行：{err.code} {err.message}")

    validation = _public_validation(checked)
    data = dict(result.data)
    data["validation"] = validation
    shipment = store.upsert_shipment(
        {
            "order_id": oid,
            "direction": "forward",
            "carrier": carrier,
            "tracking_no": tracking_no,
            "status": "in_transit" if data.get("accepted") else "created",
            "tracking_validated": True,
            "shipped_at": datetime.now(timezone.utc).isoformat(),
        },
        tracking_approval_id=claimed["id"],
    )
    if shipment is None:
        raise DraftError("物流执行结果已失去当前审批身份；外部调用审计已保留，请人工核对。")
    # 幂等回放同样是「已确认的外部结果」：必须把审批推进到 executed，
    # 否则重审同一审批时会永远停在 executing。
    try:
        apply_approval_event(
            store,
            claimed["id"],
            ApprovalEvent.EXECUTE_OK,
            execution_ref=str(write.get("id") or ""),
        )
    except ApprovalError as exc:
        raise DraftError(
            f"物流已回传，但审批状态已变（{exc}）；已保留平台执行审计，请刷新核对。"
        ) from exc
    saved = store.update_tracking_result(
        oid, approval_id=claimed["id"], payload=payload, status="executed", result=data,
    )
    if saved is None:
        raise DraftError("物流草稿身份已变；已保留平台执行审计，请刷新核对。")
    return {
        "draft": saved,
        "approval": store.get_approval(claimed["id"]),
        "shipment": shipment,
        "adapter": data,
        "idempotent_replay": bool(outcome.replayed),
        "validation": validation,
        "note": _success_note(data, validation),
    }


def _tracking_snapshot(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "order_id": str(payload.get("order_id") or "").strip(),
        "carrier": canonical_carrier(payload.get("carrier")),
        "tracking_no": str(payload.get("tracking_no") or "").strip().upper(),
        "external_po_id": str(payload.get("external_po_id") or "").strip(),
        "shipping_provider_id": str(payload.get("shipping_provider_id") or "").strip(),
    }


def _approved_tracking_snapshot(approval: dict[str, Any]) -> dict[str, str] | None:
    """核对业务签名；来源元数据保留在既有不可变快照中。"""
    body = approval.get("payload") or {}
    snapshot = _tracking_snapshot(body)
    if approval.get("payload_hash") == payload_hash(snapshot):
        return snapshot
    if body.get("source") in {"dropship_result", "override"}:
        legacy_keys = ["order_id", "carrier", "tracking_no", "external_po_id", "source"]
        if "shipping_provider_id" in body:
            legacy_keys.append("shipping_provider_id")
        signed = {key: body.get(key) for key in legacy_keys}
        if approval.get("payload_hash") == payload_hash(signed):
            return snapshot
    return None


def _ensure_tracking_approval(
    store: WorkflowStore, order_id: str, payload: dict[str, Any], *, expected: dict[str, Any] | None,
) -> dict[str, Any]:
    approval = store.get_approval(expected["approval_id"]) if expected and expected.get("approval_id") else None
    same = bool(expected and approval
                and _tracking_snapshot(expected.get("payload") or {}) == payload
                and _approved_tracking_snapshot(approval) == payload
                and approval["action_type"] == TRACKING_ACTION
                and approval["entity_type"] == "order" and approval["entity_id"] == order_id)
    frozen = ((expected or {}).get("status") in {"executing", "executed"}
              or (approval or {}).get("status") in {"executing", "executed"})
    if frozen and not same:
        raise DraftError("物流正在执行或已完成，不能改写已批准快照。")
    reuse = same and approval["status"] in {"pending", "approved", "executing", "executed"}
    fields = decision_fields = None
    if not reuse:
        policy = store.get_gate_policy(TRACKING_ACTION)
        decision = evaluate_gate_policy(policy, action_type=TRACKING_ACTION)
        fields = {
            "shop_id": (store.get_synced_order(order_id) or {}).get("shop_id"),
            "payload_hash": payload_hash(payload),
            "policy_id": (policy or {}).get("id"),
            "decide_before": (datetime.now(timezone.utc) + DECIDE_TTL).isoformat(),
            "execute_within_seconds": int(DECIDE_TTL.total_seconds()),
        }
        decision_fields = {
            "policy_id": str((policy or {}).get("id") or "missing"),
            "policy_version": int((policy or {}).get("version") or 0),
            "input_snapshot": {"order_id": order_id, "action": TRACKING_ACTION},
            "decision": decision["decision"], "reason": decision["reason"],
        }
    saved = store.revise_tracking_draft(
        order_id, expected=expected, expected_approval=approval, payload=payload,
        approval_fields=fields, gate_decision=decision_fields,
    )
    if saved is None:
        raise DraftError("物流快照或审批状态已变，请刷新后重试。")
    return saved
