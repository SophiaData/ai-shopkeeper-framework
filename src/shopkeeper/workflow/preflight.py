from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shopkeeper.adapters.alibaba_dropship import dropship_mode_label
from shopkeeper.adapters.tiktok_shop import credentials_from_env
from shopkeeper.workflow.backup import backup_dir, latest_valid_backup_stamp
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, get_schema_version, open_connection
from shopkeeper.workflow.service import WorkflowService


def _check(checks: list[dict[str, Any]], cid: str, status: str, detail: str) -> None:
    checks.append({"id": cid, "status": status, "detail": detail})


def run_preflight(env: dict[str, str] | None = None, *, require_real: bool = False) -> dict[str, Any]:
    source = dict(env if env is not None else os.environ)
    checks: list[dict[str, Any]] = []
    db_path = Path(source.get("WORKFLOW_DB_PATH", "artifacts/workflow.db"))
    if not db_path.is_file():
        _check(checks, "db", "fail", f"missing db: {db_path} (preflight is read-only; run is not created)")
        return {"ok": False, "require_real": require_real, "checks": checks}
    try:
        service = WorkflowService.from_env(source, read_only=True)
    except Exception as exc:
        _check(checks, "db", "fail", f"cannot open db read-only: {exc}")
        return {"ok": False, "require_real": require_real, "checks": checks}
    store = service.store

    try:
        conn = open_connection(store.path, read_only=True)
        version = get_schema_version(conn)
        conn.close()
        ok = version == CURRENT_SCHEMA_VERSION
        _check(checks, "db", "pass" if ok else "fail",
               f"schema v{version} / expected v{CURRENT_SCHEMA_VERSION}")
    except Exception as exc:
        _check(checks, "db", "fail", f"db error: {exc}")

    try:
        jobs = store.job_health()
        status = "pass" if jobs.get("dead", 0) == 0 else "fail"
        _check(checks, "queue", status,
               f"pending={jobs.get('pending', 0)} running={jobs.get('running', 0)} dead={jobs.get('dead', 0)}")
    except Exception as exc:
        _check(checks, "queue", "fail", f"queue error: {exc}")

    try:
        workers = store.worker_health()
        require_worker = str(source.get("WORKFLOW_REQUIRE_WORKER", "0")).strip().lower() in {"1", "true", "yes", "on"}
        if workers.get("alive", 0) > 0:
            _check(checks, "worker", "pass", f"alive={workers.get('alive', 0)}")
        elif require_worker:
            _check(checks, "worker", "fail", "WORKFLOW_REQUIRE_WORKER=1 but no fresh worker heartbeat")
        else:
            _check(checks, "worker", "warn", "no fresh worker heartbeat (inline worker may be disabled)")
    except Exception as exc:
        _check(checks, "worker", "fail", f"worker health error: {exc}")

    try:
        status = service.status()
        mode = str(status.get("tiktok_mode") or getattr(service.platform, "name", "unknown"))
        if mode in {"live", "tiktok_live"}:
            _check(checks, "platform", "pass", f"tiktok_mode={mode}")
        elif require_real:
            _check(checks, "platform", "fail", f"tiktok_mode={mode}; real run requires live mode")
        else:
            _check(checks, "platform", "warn", f"tiktok_mode={mode}; real writes are not armed")
    except Exception as exc:
        _check(checks, "platform", "fail", f"status error: {exc}")
        status = {}

    try:
        creds = credentials_from_env(source)
        if creds is not None and creds.configured:
            _check(checks, "tiktok_credentials", "pass", "TIKTOK_APP_KEY/SECRET/ACCESS_TOKEN present")
        elif require_real:
            _check(checks, "tiktok_credentials", "fail", "missing TikTok credentials")
        else:
            _check(checks, "tiktok_credentials", "warn", "missing TikTok credentials")
    except Exception as exc:
        _check(checks, "tiktok_credentials", "fail", f"credential check error: {exc}")

    try:
        armed = bool(status.get("tts_write_armed"))
        if armed:
            _check(checks, "write_gate", "pass", "TTS write gate armed (approval still required)")
        elif require_real:
            _check(checks, "write_gate", "fail", "TTS write gate is closed")
        else:
            _check(checks, "write_gate", "warn", "TTS write gate is closed")
    except Exception as exc:
        _check(checks, "write_gate", "fail", f"write gate error: {exc}")

    try:
        alibaba = dropship_mode_label(source)
        if require_real:
            _check(checks, "alibaba_fulfillment", "fail",
                   f"mode={alibaba}; live 1688 order adapter is not implemented (manual/mock only)")
        else:
            _check(checks, "alibaba_fulfillment", "warn", f"mode={alibaba}; manual/mock only")
    except Exception as exc:
        _check(checks, "alibaba_fulfillment", "fail", f"alibaba mode error: {exc}")

    try:
        provider_id = str(source.get("TIKTOK_SHIPPING_PROVIDER_ID", "")).strip()
        if provider_id:
            _check(checks, "tracking_write", "pass", "TIKTOK_SHIPPING_PROVIDER_ID configured")
        elif require_real:
            _check(checks, "tracking_write", "fail", "missing TIKTOK_SHIPPING_PROVIDER_ID")
        else:
            _check(checks, "tracking_write", "warn", "missing TIKTOK_SHIPPING_PROVIDER_ID")
    except Exception as exc:
        _check(checks, "tracking_write", "fail", f"tracking config error: {exc}")

    if require_real:
        _check(checks, "customer_service", "fail",
               "customer service inbox/send is still local stub; no real TikTok CS API")
    else:
        _check(checks, "customer_service", "warn",
               "customer service inbox/send is local stub (not connected)")

    try:
        pending = store.count_approvals(status="pending")
        _check(checks, "approvals", "info" if pending == 0 else "warn",
               f"pending approvals={pending}")
    except Exception as exc:
        _check(checks, "approvals", "fail", f"approval count error: {exc}")

    try:
        root = backup_dir(source)
        stamp = latest_valid_backup_stamp(root)
        if stamp:
            try:
                ts = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                age_h = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600)
            except ValueError:
                age_h = -1.0
            max_age_h = float(source.get("WORKFLOW_BACKUP_MAX_AGE_S", "129600")) / 3600
            if 0 <= age_h <= max_age_h:
                _check(checks, "backup", "pass", f"latest valid backup={stamp} age={age_h:.1f}h")
            elif require_real:
                _check(checks, "backup", "fail", f"latest valid backup too old or invalid: {stamp}")
            else:
                _check(checks, "backup", "warn", f"latest valid backup too old or invalid: {stamp}")
        elif require_real:
            _check(checks, "backup", "fail", "no valid backup with manifest+db found")
        else:
            _check(checks, "backup", "warn", "no valid backup with manifest+db found")
    except Exception as exc:
        _check(checks, "backup", "fail", f"backup check error: {exc}")

    failed = any(item["status"] == "fail" for item in checks)
    return {"ok": not failed, "require_real": require_real, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shopkeeper preflight (read-only)")
    parser.add_argument("--require-real", action="store_true", help="fail unless real credentials/write gate/backup are ready")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    result = run_preflight(require_real=args.require_real)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["checks"]:
            print(f"[{item['status'].upper():4}] {item['id']}: {item['detail']}")
        print("PREFLIGHT PASSED" if result["ok"] else "PREFLIGHT FAILED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
