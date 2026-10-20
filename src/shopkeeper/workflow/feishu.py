"""飞书审批互动卡片 + 回调（DESIGN §5 `/feishu/approval-callback`）。

- 创建审批单时可发互动卡片：默认 `FEISHU_NOTIFY=0`，写入 `artifacts/feishu/cards/`，不外呼。
- 回调验签（`FEISHU_ENCRYPT_KEY` / `FEISHU_VERIFICATION_TOKEN`）；失败 401。
- 卡片按钮 / mock 事件 → approve|reject → 推进八态闸门并投影草稿。
凭据只读环境变量，禁止写入代码或文档。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.logging_setup import get_logger
from shopkeeper.workflow.approvals import ApprovalError, apply_approval_event
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.workflow.feishu")


class FeishuCallbackError(Exception):
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


def _env(env: dict[str, str] | None = None) -> dict[str, str]:
    return env if env is not None else os.environ


def verification_token(env: dict[str, str] | None = None) -> str:
    source = _env(env)
    return str(source.get("FEISHU_VERIFICATION_TOKEN") or source.get("FEISHU_VERIFY_TOKEN") or "").strip()


def encrypt_key(env: dict[str, str] | None = None) -> str:
    source = _env(env)
    return str(source.get("FEISHU_ENCRYPT_KEY") or "").strip()


def notify_enabled(env: dict[str, str] | None = None) -> bool:
    raw = str(_env(env).get("FEISHU_NOTIFY", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def card_dir(env: dict[str, str] | None = None) -> Path:
    source = _env(env)
    raw = str(source.get("FEISHU_CARD_DIR") or "artifacts/feishu/cards").strip()
    return Path(raw)


def webhook_url(env: dict[str, str] | None = None) -> str:
    return str(_env(env).get("FEISHU_WEBHOOK_URL") or "").strip()


def compute_signature(timestamp: str, nonce: str, key: str, body: bytes | str) -> str:
    """飞书事件订阅签名：sha256(timestamp + nonce + encrypt_key + body)。"""
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    material = f"{timestamp}{nonce}{key}".encode("utf-8") + raw
    return hashlib.sha256(material).hexdigest()


def verify_request(
    *,
    timestamp: str,
    nonce: str,
    signature: str,
    body: bytes,
    env: dict[str, str] | None = None,
    now: float | None = None,
) -> None:
    # ⚠️ 重放窗**先于**凭证判定（独立复核发现，已复现）。
    #
    # 原实现**只验签名，不看时间**。而签名绑定 `(timestamp, nonce, key, body)` ——
    # 攻击者只要抓到一次合法回调（代理日志、SSRF 落点、中间人），
    # 就能原样重放，签名**永远有效**。
    #
    # 实测（修复前）：`timestamp` 取 `1000000000`（2001 年）、`1`、**空串**、
    # `9999999999999`（2286 年）**全部接受**。`nonce` 也只被拼进 MAC，
    # 从不存储、从不比对 —— 防不了重放。
    #
    # 复用 TikTok 侧已有的官方重放窗（「不早于 5 分钟、不晚于 30 秒」），
    # 不另立第二套口径。
    #
    # 顺序：放在凭证判定**之前** —— 否则「未配置」会变成一条静默的时间窗旁路。
    import time as _time

    from shopkeeper.adapters.tiktok_shop import is_timestamp_in_window

    clock = _time.time() if now is None else float(now)
    if not is_timestamp_in_window(timestamp, clock):
        raise FeishuCallbackError("飞书回调时间戳超出重放窗（或缺失/非法）。")

    key = encrypt_key(env)
    token = verification_token(env)
    if not key and not token:
        if str(_env(env).get("FEISHU_CALLBACK_ALLOW_INSECURE") or "") == "1":
            return
        raise FeishuCallbackError("未配置 FEISHU_ENCRYPT_KEY / FEISHU_VERIFICATION_TOKEN，拒绝回调。")
    if key:
        expect = compute_signature(timestamp, nonce, key, body)
        if not hmac.compare_digest(expect, str(signature or "")):
            raise FeishuCallbackError("飞书回调签名校验失败。")
        return
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise FeishuCallbackError("回调 body 不是 JSON。") from exc
    # 用**常量时间比较**：这是与签名校验并列的同一类共享密钥比对，
    # 上面那段 HMAC 分支已经用了 `hmac.compare_digest`，这里却用 `!=` ——
    # 同样是逐字节短路，会泄漏「前几位对上了」的时序信息。
    # 网络路径上可利用性不高，但**同一份文件里两种写法**本身就是隐患：
    # 后来的人会照抄更简单的那种。
    if not hmac.compare_digest(str(payload.get("token") or ""), str(token or "")):
        raise FeishuCallbackError("飞书 verification token 不匹配。")


def build_approval_card(
    approval: dict[str, Any],
    *,
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """互动卡片：标题、摘要、批准/拒绝按钮（value 带 approval_id）。

    ## ⚠️ 出站前必须过脱敏守卫（独立复核发现，已复现）

    卡片会**发给飞书**（第三方），而原实现直接读原始 dict 的
    `draft.title` / `draft.body_th` —— `guard_payload` 在这条路径上**从未被调用**
    （全仓只有 `pii_retention.py` 引用它）。

    实测（修复前）：草稿带买家姓名/泰国手机号/邮箱时，卡片原文带出：

    ```
    待审批 · Somchai Jaidee
    摘要：ติดต่อคุณสมชาย โทร 0812345678 อีเมล somchai@example.com
    ```

    **明确局限**：`title` 在守卫里是**直通字段**（商品标题本就要外发），
    所以「买家姓名塞进 title」这一形态守卫**抓不到** ——
    守卫是必要条件，不是充分条件。
    """
    from shopkeeper.security.redaction import guard_payload

    safe_key = str(approval.get("entity_id") or approval.get("id") or "")
    if draft:
        draft, _rep = guard_payload(draft, row_key=f"feishu.draft.{safe_key}", sniff=True)
    payload_raw = approval.get("payload")
    payload_safe: dict[str, Any] = {}
    if isinstance(payload_raw, dict):
        payload_safe, _rep2 = guard_payload(
            payload_raw, row_key=f"feishu.payload.{safe_key}", sniff=True
        )

    title = str(
        (draft or {}).get("title")
        or payload_safe.get("title")
        or approval.get("action_type")
        or "审批"
    )
    action_type = str(approval.get("action_type") or "")
    entity_id = str(approval.get("entity_id") or "")
    summary_parts = [
        f"动作：{action_type}",
        f"实体：{approval.get('entity_type')}/{entity_id}",
        f"审批单：{approval.get('id')}",
        f"截止：{approval.get('decide_before') or ''}",
    ]
    if draft:
        body_th = str(draft.get("body_th") or "")[:120]
        if body_th:
            summary_parts.append(f"摘要：{body_th}")
    summary = "\n".join(summary_parts)
    approval_id = str(approval["id"])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"待审批 · {title}"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": summary}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "type": "primary",
                        "value": {"action": "approve", "approval_id": approval_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {"action": "reject", "approval_id": approval_id},
                    },
                ],
            },
        ],
    }


def send_approval_card(
    store: WorkflowStore,
    approval: dict[str, Any],
    *,
    draft: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """发送审批卡片。默认不外呼：落盘 stub。缺配置不抛错。"""
    draft = draft or (
        store.get_draft(str(approval["entity_id"]))
        if approval.get("entity_type") == "draft"
        else None
    )
    card = build_approval_card(approval, draft=draft)
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "approval_id": approval.get("id"),
        "action_type": approval.get("action_type"),
        "entity_id": approval.get("entity_id"),
        "notify_enabled": notify_enabled(env),
        "mode": "stub",
        "card": card,
        "outbound": False,
        "note": "",
    }
    # 始终先落盘（可审计、可测）；外呼仅在显式 FEISHU_NOTIFY=1 且配置了 webhook 时尝试
    out_dir = card_dir(env)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{approval['id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        record["stub_path"] = str(path)
    except OSError as exc:
        record["note"] = f"本地 stub 写入失败（已降级）：{exc}"
        record["stub_path"] = ""

    if not notify_enabled(env):
        record["note"] = record.get("note") or "FEISHU_NOTIFY=0：已写本地 stub，未外呼。"
        _attach_notify_meta(store, approval, record)
        return record

    url = webhook_url(env)
    if not url:
        record["note"] = "FEISHU_NOTIFY=1 但未配置 FEISHU_WEBHOOK_URL：仅本地 stub，不外呼。"
        _attach_notify_meta(store, approval, record)
        return record

    # ⚠️ 出站前过与 `alerts.py` 同一道 SSRF 守卫（独立复核发现）。
    #
    # `alerts._post_json` 走 `jobs.webhook_refusal`（只允许 https、拒回环/内网/云元数据），
    # 而这里**直接 httpx.post**，没有 scheme/host 检查。
    # 实测：`https://127.0.0.1:8000/steal` 与 `http://169.254.169.254/...`
    # **都会被真的发出请求** —— 而卡片里含审批信息。
    # 同一条出站规则只应有一个实现 —— 复用，不另写一遍。
    from shopkeeper.workflow.jobs import webhook_refusal

    refusal = webhook_refusal(url)
    if refusal:
        record["note"] = f"FEISHU_WEBHOOK_URL 被拒绝（{refusal}）：仅本地 stub，不外呼。"
        _attach_notify_meta(store, approval, record)
        return record

    # 可选出站：失败不炸，记入 stub；传输类可有限重试后降级
    import httpx

    from shopkeeper.errors import AdapterError
    from shopkeeper.models import AdapterResult
    from shopkeeper.retry import call_with_retry, retry_config_from_env

    def _post_once() -> AdapterResult[httpx.Response]:
        try:
            resp = httpx.post(
                url, json={"msg_type": "interactive", "card": card}, timeout=10.0
            )
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.generation("FEISHU_TRANSPORT", "飞书 webhook 超时"),
                "feishu",
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation(
                    "FEISHU_TRANSPORT", f"飞书 webhook 传输失败：{type(exc).__name__}"
                ),
                "feishu",
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.failure(
                AdapterError.generation(
                    "FEISHU_TRANSPORT", f"飞书 webhook 异常：{type(exc).__name__}"
                ),
                "feishu",
            )
        if resp.status_code >= 500:
            return AdapterResult.failure(
                AdapterError.generation(
                    "FEISHU_TRANSPORT", f"飞书 webhook HTTP {resp.status_code}"
                ),
                "feishu",
            )
        return AdapterResult.success(resp, "feishu")

    http_res, attempts = call_with_retry(_post_once, config=retry_config_from_env())
    record["retry_attempts"] = attempts
    if http_res.ok and http_res.data is not None:
        resp = http_res.data
        record["outbound"] = True
        record["mode"] = "webhook"
        record["http_status"] = resp.status_code
        record["note"] = "已尝试 webhook 外呼。"
        if resp.status_code >= 400:
            log.error(
                "feishu notify http failed approval_id=%s status=%s",
                approval.get("id"),
                resp.status_code,
            )
            _alert_feishu_failure(str(approval.get("entity_id") or ""))
    else:
        err = http_res.error
        detail = err.message if err is not None else "transport failed"
        record["note"] = f"webhook 外呼失败，已降级 stub：{detail}"
        log.error(
            "feishu notify transport failed approval_id=%s err=%s attempts=%s",
            approval.get("id"),
            detail,
            attempts,
        )
        _alert_feishu_failure(str(approval.get("entity_id") or ""))
    if record.get("stub_path"):
        try:
            Path(record["stub_path"]).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
    _attach_notify_meta(store, approval, record)
    return record


def _alert_feishu_failure(order_id: str) -> None:
    from shopkeeper.workflow.alerts import CODE_FEISHU_NOTIFY_FAILED, emit_alert

    emit_alert(CODE_FEISHU_NOTIFY_FAILED, job_type="feishu_notify", order_id=order_id)


def _attach_notify_meta(store: WorkflowStore, approval: dict[str, Any], record: dict[str, Any]) -> None:
    """把通知结果记进审批 payload（无密钥）。失败静默。"""
    try:
        row = store.get_approval(str(approval["id"]))
        if row is None:
            return
        payload = dict(row.get("payload") or {})
        payload["feishu_notify"] = {
            "at": record.get("at"),
            "mode": record.get("mode"),
            "stub_path": record.get("stub_path") or "",
            "outbound": bool(record.get("outbound")),
            "note": record.get("note") or "",
        }
        store.update_approval_payload(str(approval["id"]), payload)
    except Exception:  # noqa: BLE001
        return


def handle_callback(
    store: WorkflowStore,
    payload: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """处理 challenge、卡片按钮、或 mock 审批事件。"""
    if payload.get("type") == "url_verification" or (
        "challenge" in payload and payload.get("type") != "event_callback"
    ):
        challenge = payload.get("challenge")
        token = verification_token(env)
        got = str(payload.get("token") or "")
        if token and got and not hmac.compare_digest(got, str(token)):
            raise FeishuCallbackError("challenge token 不匹配。")
        return {"challenge": challenge}

    action, approval_id, note, user_id = _extract_action(payload)
    if action in {"approve", "reject"} and approval_id:
        event = ApprovalEvent.APPROVE if action == "approve" else ApprovalEvent.REJECT
        return _decide(store, approval_id, event, note=note, user_id=user_id)

    return {
        "ok": True,
        "handled": False,
        "event_type": _event_type(payload),
        "note": "未识别的飞书事件；已支持卡片按钮与 approval.approve|reject。",
    }


def _event_type(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    return str(
        header.get("event_type")
        or payload.get("event_type")
        or event.get("type")
        or payload.get("type")
        or ""
    )


def _extract_action(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    """从卡片回调或简易 mock 体抽出 action / approval_id。"""
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_type = _event_type(payload)

    # 互动卡片按钮（card.action.trigger / card.action）
    action_block = event.get("action") if isinstance(event.get("action"), dict) else {}
    value = action_block.get("value") if isinstance(action_block.get("value"), dict) else {}
    if not value and isinstance(payload.get("action"), dict):
        action_block = payload["action"]
        value = action_block.get("value") if isinstance(action_block.get("value"), dict) else {}
    if value:
        act = str(value.get("action") or value.get("option") or "").strip().lower()
        aid = str(value.get("approval_id") or "").strip()
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        user = str(operator.get("open_id") or operator.get("user_id") or "feishu")
        if act in {"approve", "reject"} and aid:
            return act, aid, f"feishu-card:{act}", user

    # 简易 mock / 旧骨架
    if event_type in {"approval.approve", "approval_instance.approve", "approve"}:
        body = event if event else payload
        return (
            "approve",
            str(body.get("approval_id") or body.get("instance_code") or "").strip(),
            str(body.get("note") or body.get("comment") or "feishu"),
            str(body.get("user_id") or "feishu"),
        )
    if event_type in {"approval.rejected", "approval.reject", "approval_instance.reject", "reject"}:
        body = event if event else payload
        return (
            "reject",
            str(body.get("approval_id") or body.get("instance_code") or "").strip(),
            str(body.get("note") or body.get("comment") or "feishu"),
            str(body.get("user_id") or "feishu"),
        )

    # 顶层简写
    act = str(payload.get("action") or "").strip().lower()
    aid = str(payload.get("approval_id") or "").strip()
    if act in {"approve", "reject"} and aid:
        return act, aid, str(payload.get("note") or "feishu"), str(payload.get("user_id") or "feishu")

    _ = header  # silence lint for unused in some paths
    return "", "", "", ""


def _decide(
    store: WorkflowStore,
    approval_id: str,
    action: ApprovalEvent,
    *,
    note: str,
    user_id: str,
) -> dict[str, Any]:
    if not approval_id:
        raise FeishuCallbackError("缺少 approval_id。", status_code=400)
    try:
        saved = apply_approval_event(
            store,
            approval_id,
            action,
            decided_by=user_id or "feishu",
            note=note,
        )
    except ApprovalError as exc:
        raise FeishuCallbackError(str(exc), status_code=409) from exc
    if saved.get("entity_type") == "draft":
        from shopkeeper.workflow.approvals import sync_draft_projection

        sync_draft_projection(store, saved["entity_id"], saved["status"], note)
    return {"ok": True, "handled": True, "approval": saved}
