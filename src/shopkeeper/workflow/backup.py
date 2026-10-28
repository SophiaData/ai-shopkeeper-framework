"""artifacts 下 SQLite / 审计 JSONL 的一致性备份与恢复。

备份用 ``sqlite3.Connection.backup``（在线热备，不依赖文件拷贝半截库）。
恢复必须显式 ``--confirm YES``，避免误覆盖生产库。

环境变量：
- ``ARTIFACTS_DIR`` 默认 ``artifacts``
- ``BACKUP_DIR`` 默认 ``artifacts/backups``
- ``BACKUP_KEEP`` 默认 ``14``
- ``BACKUP_DAILY`` 默认 ``0``（调度器侧，见 jobs）

保留策略（manifest 亦写入）：备份副本受 ``BACKUP_KEEP`` 份数裁剪；库内买家 PII
另受 ``PII_RETENTION_DAYS``（默认 90）覆写——过期后订单/客服 PII 在源库已脱敏，
但**历史备份目录仍可能含清理前明文**，须按同保留期自行轮转/销毁备份。
本说明为工程默认，**非法律意见**。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from shopkeeper.logging_setup import get_logger
from shopkeeper.workflow.migrate import (
    CONNECT_TIMEOUT_S,
    CURRENT_SCHEMA_VERSION,
    configure_connection,
    get_schema_version,
)

log = get_logger("shopkeeper.workflow.backup")

DEFAULT_ARTIFACTS_DIR = "artifacts"
DEFAULT_BACKUP_DIR = "artifacts/backups"
DEFAULT_KEEP = 14
DEFAULT_WORKFLOW_DB = "artifacts/workflow.db"
RESTORE_CONFIRM = "YES"


class BackupError(RuntimeError):
    """备份/恢复失败（消息可直接给 CLI）。"""


def _env(env: dict[str, str] | None = None) -> dict[str, str]:
    return env if env is not None else os.environ


def artifacts_dir(env: dict[str, str] | None = None) -> Path:
    raw = str(_env(env).get("ARTIFACTS_DIR") or DEFAULT_ARTIFACTS_DIR).strip() or DEFAULT_ARTIFACTS_DIR
    return Path(raw)


def backup_dir(env: dict[str, str] | None = None) -> Path:
    raw = str(_env(env).get("BACKUP_DIR") or DEFAULT_BACKUP_DIR).strip() or DEFAULT_BACKUP_DIR
    return Path(raw)


def backup_keep(env: dict[str, str] | None = None) -> int:
    raw = str(_env(env).get("BACKUP_KEEP") or DEFAULT_KEEP).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_KEEP
    return max(1, value)


def backup_daily_enabled(env: dict[str, str] | None = None) -> bool:
    raw = str(_env(env).get("BACKUP_DAILY", "") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def collect_db_files(artifacts: Path, backups_root: Path) -> list[Path]:
    """收集 artifacts 下全部 .db，排除备份目录自身。"""
    if not artifacts.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(artifacts.rglob("*.db")):
        if not path.is_file():
            continue
        if _is_under(path, backups_root):
            continue
        out.append(path)
    return out


def collect_audit_jsonl(artifacts: Path, backups_root: Path) -> list[Path]:
    """收集审计 JSONL（文件名含 audit，后缀 .jsonl）。"""
    if not artifacts.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(artifacts.rglob("*.jsonl")):
        if not path.is_file():
            continue
        if _is_under(path, backups_root):
            continue
        if "audit" not in path.name.lower():
            continue
        out.append(path)
    return out


def _rel_key(path: Path, artifacts: Path) -> str:
    """相对 artifacts 的 POSIX 风格键，保证 Windows 路径在清单里可移植。"""
    try:
        rel = path.resolve().relative_to(artifacts.resolve())
    except ValueError:
        rel = Path(path.name)
    return rel.as_posix()


def sqlite_consistent_copy(src: Path, dst: Path) -> None:
    """用 sqlite3 backup API 做一致性拷贝。

    不先 ``unlink`` 目标文件：Windows 上 WAL/未释放句柄会导致 WinError 32；
    backup API 会原地覆盖目标库内容。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src), timeout=CONNECT_TIMEOUT_S)
    try:
        configure_connection(src_conn)
        dst_conn = sqlite3.connect(str(dst), timeout=CONNECT_TIMEOUT_S)
        try:
            configure_connection(dst_conn)
            src_conn.backup(dst_conn)
            dst_conn.commit()
            # 尽量收口 WAL，降低 Windows 上后续替换/删除的锁冲突
            try:
                dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def list_backup_stamps(backups_root: Path) -> list[str]:
    if not backups_root.is_dir():
        return []
    stamps = [p.name for p in backups_root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return sorted(stamps)


def _sqlite_backup_ok(path: Path) -> bool:
    """只读打开并确认是带 schema_version 的可用 SQLite 库。"""
    try:
        conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                return False
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def backup_stamp_is_valid(stamp_dir: Path) -> bool:
    """目录必须是真实备份：manifest、工作台库条目、SQLite 可读且含 schema_version。"""
    directory = Path(stamp_dir)
    manifest = directory / "manifest.json"
    if not directory.is_dir() or not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entries = data.get("files") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return False
    db_entries = [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("kind") == "db" and entry.get("rel")
    ]
    if not db_entries:
        return False
    workflow_rel = str((data or {}).get("workflow_db") or "")
    if workflow_rel:
        entry = next((e for e in db_entries if str(e.get("rel")) == workflow_rel), None)
    else:
        entry = next(
            (e for e in db_entries if Path(str(e.get("rel"))).name == "workflow.db"),
            None,
        )
        if entry is None and len(db_entries) == 1:
            entry = db_entries[0]
    if entry is None:
        return False
    candidate = directory / Path(*str(entry["rel"]).split("/"))
    try:
        if not candidate.resolve().is_relative_to(directory.resolve()):
            return False
    except (OSError, ValueError):
        return False
    return (
        candidate.is_file()
        and candidate.stat().st_size > 0
        and _sqlite_backup_ok(candidate)
    )


def latest_valid_backup_stamp(backups_root: Path) -> str | None:
    for stamp in reversed(list_backup_stamps(Path(backups_root))):
        if backup_stamp_is_valid(Path(backups_root) / stamp):
            return stamp
    return None


def prune_backups(backups_root: Path, keep: int) -> list[str]:
    """保留最近 keep 份（按目录名时间戳排序），删除更旧的。返回被删 stamp。"""
    stamps = list_backup_stamps(backups_root)
    if len(stamps) <= keep:
        return []
    removed: list[str] = []
    for stamp in stamps[: len(stamps) - keep]:
        target = backups_root / stamp
        shutil.rmtree(target)
        removed.append(stamp)
        log.info("backup pruned stamp=%s", stamp)
    return removed


def create_backup(
    *,
    artifacts: Path | None = None,
    backups_root: Path | None = None,
    keep: int | None = None,
    env: dict[str, str] | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    """备份全部 .db 与审计 jsonl，写入带时间戳的目录，并裁剪旧份。"""
    art = Path(artifacts) if artifacts is not None else artifacts_dir(env)
    root = Path(backups_root) if backups_root is not None else backup_dir(env)
    keep_n = int(keep) if keep is not None else backup_keep(env)
    art.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)

    stamp_value = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = root / stamp_value
    if dest.exists():
        raise BackupError(f"备份目录已存在：{dest}")
    dest.mkdir(parents=True, exist_ok=False)

    db_files = collect_db_files(art, root)
    audit_files = collect_audit_jsonl(art, root)
    copied: list[dict[str, str]] = []
    workflow_db_rel: str | None = None
    workflow_name = Path(
        str(_env(env).get("WORKFLOW_DB_PATH") or DEFAULT_WORKFLOW_DB)
    ).name

    for src in db_files:
        key = _rel_key(src, art)
        target = dest / Path(*key.split("/"))
        sqlite_consistent_copy(src, target)
        copied.append({"kind": "db", "rel": key, "bytes": str(target.stat().st_size)})
        if workflow_db_rel is None and Path(key).name == workflow_name:
            workflow_db_rel = key

    for src in audit_files:
        key = _rel_key(src, art)
        target = dest / Path(*key.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append({"kind": "jsonl", "rel": key, "bytes": str(target.stat().st_size)})

    pii_days_raw = str(_env(env).get("PII_RETENTION_DAYS") or "90").strip()
    try:
        pii_days = max(1, int(pii_days_raw))
    except ValueError:
        pii_days = 90
    manifest = {
        "stamp": stamp_value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifacts_dir": str(art),
        "workflow_db": workflow_db_rel,
        "files": copied,
        "retention": {
            "backup_keep": keep_n,
            "pii_retention_days": pii_days,
            "note": (
                "Backup copies are pruned by backup_keep. Buyer PII in the live DB is "
                "overwritten after pii_retention_days (see shopkeeper.workflow.pii_retention). "
                "Older backup stamps may still contain pre-purge PII — rotate/destroy them "
                "under the same retention policy. Not legal advice."
            ),
        },
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    pruned = prune_backups(root, keep_n)
    log.info(
        "backup done stamp=%s db=%s jsonl=%s pruned=%s",
        stamp_value,
        len(db_files),
        len(audit_files),
        len(pruned),
    )
    return {
        "stamp": stamp_value,
        "path": str(dest),
        "files": copied,
        "pruned": pruned,
        "keep": keep_n,
    }


def verify_restored_schema(path: Path) -> dict[str, Any]:
    """校验恢复出来的 SQLite 库能否被当前代码使用（P1-03）。

    为什么需要它：备份是**某个时间点**的库，代码却在往前走。把一份 v8 备份恢复到
    期望 v9 的代码上，此前会「静默成功」——库少三列（例如 P0-07 的
    `platform_audit_status`），直到某个查询炸在运行时才被发现，而那时生产库已经被
    覆盖了。恢复是破坏性操作，必须在覆盖前后给出明确结论。

    返回 ``{path, schema_version, expected, status, problems}``，``status`` 取：
      - ``ok``：版本一致
      - ``behind``：备份比代码旧，**可由 apply_migrations 前滚**（不是错误，但要提示）
      - ``ahead``：备份比代码新——降级代码读新库，必须拒绝
      - ``missing``：连 schema_version 表都没有，可能不是工作台库
    """
    problems: list[str] = []
    version: int | None = None
    try:
        conn = sqlite3.connect(str(path))
        try:
            configure_connection(conn)
            has_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if has_table is None:
                return {
                    "path": str(path),
                    "schema_version": None,
                    "expected": CURRENT_SCHEMA_VERSION,
                    "status": "missing",
                    "problems": ["没有 schema_version 表，可能不是工作台库。"],
                }
            version = get_schema_version(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "path": str(path),
            "schema_version": None,
            "expected": CURRENT_SCHEMA_VERSION,
            "status": "missing",
            "problems": [f"无法读取该库：{exc}"],
        }

    if version == CURRENT_SCHEMA_VERSION:
        status = "ok"
    elif version < CURRENT_SCHEMA_VERSION:
        status = "behind"
        problems.append(
            f"备份库 schema v{version} 低于当前代码期望的 v{CURRENT_SCHEMA_VERSION}；"
            "启动时会由 apply_migrations 前滚（幂等）。恢复本身可用，但请确认这是预期版本。"
        )
    else:
        status = "ahead"
        problems.append(
            f"备份库 schema v{version} 高于当前代码期望的 v{CURRENT_SCHEMA_VERSION}；"
            "用旧代码读新库会缺列或语义不符，**应先用匹配版本的代码**。"
        )

    return {
        "path": str(path),
        "schema_version": version,
        "expected": CURRENT_SCHEMA_VERSION,
        "status": status,
        "problems": problems,
    }


def _sqlite_quick_check(path: Path) -> tuple[bool, str]:
    """只读打开库做 ``PRAGMA quick_check``，返回 ``(是否完好, 详情)``。

    损坏的备份必须在**写盘之前**被识别：把损坏文件拷进生产库，等于把
    「可回滚」变成「不可回滚」。
    """
    try:
        conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"无法读取该库：{exc}"
    if not row:
        return False, "quick_check 没有返回结果。"
    detail = str(row[0])
    return detail.lower() == "ok", detail


def _has_schema_version(path: Path) -> bool:
    """库里是否有 ``schema_version`` 表（用来认工作台主库）。"""
    try:
        probe = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True)
        try:
            row = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
        finally:
            probe.close()
        return row is not None
    except sqlite3.Error:
        return False


def _read_manifest_entries(src_dir: Path) -> list[dict[str, Any]]:
    """从 manifest（或目录扫描兜底）读出待恢复条目。"""
    manifest_path = src_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return list(manifest.get("files") or [])
    entries: list[dict[str, Any]] = []
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        rel = path.relative_to(src_dir).as_posix()
        kind = "db" if path.suffix.lower() == ".db" else "jsonl"
        entries.append({"kind": kind, "rel": rel})
    return entries


def _plan_restore(
    entries: list[dict[str, Any]],
    src_dir: Path,
    art: Path,
    main_db_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """**只读**地规划一次恢复：校验路径合法性、文件存在性、库完整性与 schema 版本。

    返回 ``(计划, schema 检查, 阻断原因)``。任何阻断原因非空时，调用方**不得**
    写生产库 —— 这是 O-21 的核心契约：拒绝必须发生在覆盖之前。
    """
    planned: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    blocking: list[str] = []

    for entry in entries:
        rel = str(entry.get("rel") or "")
        if not rel or rel in {".", ".."} or ".." in Path(rel).parts:
            blocking.append(f"非法相对路径：{rel!r}")
            continue
        src = src_dir / Path(*rel.split("/"))
        if not src.is_file():
            blocking.append(f"备份缺少文件：{src}")
            continue
        kind = str(entry.get("kind") or ("db" if src.suffix.lower() == ".db" else "jsonl"))
        planned.append({"kind": kind, "rel": rel, "src": src, "dst": art / Path(*rel.split("/"))})

    # 完整性 + 版本校验都针对**备份源文件**，不碰生产库。
    for item in planned:
        if item["kind"] != "db":
            continue
        src: Path = item["src"]
        ok, detail = _sqlite_quick_check(src)
        if not ok:
            blocking.append(f"{item['rel']}: 备份文件损坏（quick_check={detail}），拒绝恢复。")
            continue
        # 只校验工作台主库：artifacts 下的普通 .db（demo 包的 shop.db 之类）
        # 没有 schema_version 表，不能拿工作台的版本标准去要求它们。
        if Path(item["rel"]).name != main_db_name and not _has_schema_version(src):
            continue
        check = verify_restored_schema(src)
        check["path"] = str(src)
        checks.append(check)
        if check["status"] == "ahead":
            blocking.append(
                f"{item['rel']}: 备份 schema v{check['schema_version']} 比代码期望的 "
                f"v{check['expected']} 新，拒绝恢复（会缺列）。"
            )
        elif check["status"] == "missing":
            for problem in check["problems"]:
                log.warning("restore schema check: %s", problem)
        elif check["status"] == "behind":
            for problem in check["problems"]:
                log.warning("restore schema check: %s", problem)

    return planned, checks, blocking


def _stage_restore(planned: list[dict[str, Any]], art: Path) -> Path:
    """把待恢复内容全部准备到临时目录，成功后才替换生产文件。

    「准备」与「替换」分开，是为了让**任何一步失败都不留下半库**：损坏文件在
    临时目录里就失败，生产库根本没被碰过。
    """
    stage_root = art / f".restore-stage-{os.getpid()}"
    if stage_root.exists():
        shutil.rmtree(stage_root, ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    try:
        for item in planned:
            src: Path = item["src"]
            staged = stage_root / Path(*str(item["rel"]).split("/"))
            staged.parent.mkdir(parents=True, exist_ok=True)
            if item["kind"] == "db":
                sqlite_consistent_copy(src, staged)
            else:
                shutil.copy2(src, staged)
            item["staged"] = staged
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise
    return stage_root


def _wal_sidecars(db_path: Path) -> tuple[Path, Path]:
    """返回 WAL 模式的两个边车文件路径（``-wal`` / ``-shm``）。"""
    return (
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )


def _snapshot_for_rollback(dst: Path, kind: str) -> Path | None:
    """替换前给生产文件留一份可回滚快照。

    库文件用 ``sqlite3 backup`` API 做**一致性**快照：直接 ``copy2`` 会漏掉
    还在 WAL 里、尚未 checkpoint 的事务内容，回滚回去就丢数据。
    """
    if not dst.exists():
        return None
    snapshot = dst.with_name(f"{dst.name}.restore-rollback")
    if kind == "db":
        sqlite_consistent_copy(dst, snapshot)
    else:
        shutil.copy2(dst, snapshot)
    return snapshot


def _commit_restore(planned: list[dict[str, Any]], art: Path, stage_root: Path) -> list[str]:
    """把暂存内容替换进生产库；任一步失败即回滚到替换前的样子。

    库文件替换时必须一并清掉旧的 ``-wal`` / ``-shm`` 边车文件：留着上一代库的
    WAL 边车，新库打开时 SQLite 会拿旧 WAL 做恢复，内容会串。
    """
    replaced: list[tuple[Path, Path | None, str]] = []
    try:
        for item in planned:
            staged: Path = item["staged"]
            dst: Path = item["dst"]
            kind = str(item["kind"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            snapshot = _snapshot_for_rollback(dst, kind)
            if kind == "db":
                wal, shm = _wal_sidecars(dst)
                wal.unlink(missing_ok=True)
                shm.unlink(missing_ok=True)
            try:
                shutil.move(str(staged), str(dst))
            except OSError:
                # move 跨设备/占用失败时退回复制
                shutil.copy2(staged, dst)
            replaced.append((dst, snapshot, kind))
    except Exception:
        # 回滚：把替换前的内容放回去，删除这次新增的文件。
        for dst, snapshot, kind in reversed(replaced):
            try:
                if snapshot is not None and snapshot.exists():
                    if kind == "db":
                        shutil.copy2(snapshot, dst)
                        wal, shm = _wal_sidecars(dst)
                        wal.unlink(missing_ok=True)
                        shm.unlink(missing_ok=True)
                    else:
                        shutil.copy2(snapshot, dst)
                    snapshot.unlink(missing_ok=True)
                else:
                    dst.unlink(missing_ok=True)
            except OSError:
                log.exception("restore rollback failed for %s", dst)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    return [str(item["rel"]) for item in planned]


def restore_backup(
    stamp: str,
    *,
    confirm: str,
    artifacts: Path | None = None,
    backups_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """从 stamp 目录恢复到 artifacts。必须 confirm=YES。

    恢复分三段：**只读校验 → 暂存准备 → 替换提交**。

    - 校验段只读备份源文件（quick_check + schema 版本），任何阻断都在**写生产库
      之前**发生。旧实现是「先覆盖、后校验」：实测把超前版本的备份恢复到一个
      有生产数据的库上时，恢复被拒绝了，但生产库已经被那份超前备份覆盖，原数据
      全丢 —— 拒绝得毫无意义。
    - 准备段把全部内容写到 ``artifacts/.restore-stage-<pid>/``；损坏文件在这一步
      就失败，生产库不受影响（旧实现抛裸 ``sqlite3.DatabaseError`` 并留下半库）。
    - 提交段才替换生产文件；替换途中失败会回滚到替换前的样子。
    """
    if str(confirm).strip() != RESTORE_CONFIRM:
        raise BackupError(f"恢复需要显式确认参数 --confirm {RESTORE_CONFIRM}")

    art = Path(artifacts) if artifacts is not None else artifacts_dir(env)
    root = Path(backups_root) if backups_root is not None else backup_dir(env)
    src_dir = root / stamp
    if not src_dir.is_dir():
        raise BackupError(f"找不到备份：{src_dir}")

    entries = _read_manifest_entries(src_dir)
    if not entries:
        raise BackupError(f"备份为空：{src_dir}")

    main_db_name = Path(str(_env(env).get("WORKFLOW_DB_PATH") or DEFAULT_WORKFLOW_DB)).name
    planned, checks, blocking = _plan_restore(entries, src_dir, art, main_db_name)
    if blocking:
        # 拒绝必须发生在写盘之前：生产库此刻仍是原样。
        raise BackupError("恢复前校验未通过（未改动生产库）：\n" + "\n".join(blocking))

    art.mkdir(parents=True, exist_ok=True)
    stage_root = _stage_restore(planned, art)
    restored = _commit_restore(planned, art, stage_root)

    for check in checks:
        for problem in check["problems"]:
            if check["status"] == "ahead":
                continue
            log.warning("restore schema check: %s", problem)

    log.info("restore done stamp=%s files=%s", stamp, len(restored))
    return {
        "stamp": stamp,
        "restored": restored,
        "artifacts_dir": str(art),
        "schema_checks": checks,
    }


def run_scheduled_backup(env: dict[str, str] | None = None) -> dict[str, Any]:
    """调度器入口：失败时抛 BackupError（由 jobs 捕获并告警）。"""
    return create_backup(env=env)


def _cmd_backup(args: argparse.Namespace) -> int:
    env = os.environ
    result = create_backup(
        artifacts=Path(args.artifacts) if args.artifacts else None,
        backups_root=Path(args.backup_dir) if args.backup_dir else None,
        keep=args.keep,
        env=env,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    result = restore_backup(
        args.stamp,
        confirm=args.confirm or "",
        artifacts=Path(args.artifacts) if args.artifacts else None,
        backups_root=Path(args.backup_dir) if args.backup_dir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    root = Path(args.backup_dir) if args.backup_dir else backup_dir()
    stamps = list_backup_stamps(root)
    print(json.dumps({"backup_dir": str(root), "stamps": stamps}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(
        prog="python -m shopkeeper.workflow.backup",
        description="备份/恢复 artifacts 下的 .db 与审计 jsonl",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="一致性备份到 BACKUP_DIR/<时间戳>")
    b.add_argument("--artifacts", default="", help="默认 ARTIFACTS_DIR 或 artifacts")
    b.add_argument("--backup-dir", default="", help="默认 BACKUP_DIR 或 artifacts/backups")
    b.add_argument("--keep", type=int, default=None, help="保留最近 N 份，默认 BACKUP_KEEP=14")
    b.set_defaults(func=_cmd_backup)

    r = sub.add_parser("restore", help="从时间戳目录恢复（须 --confirm YES）")
    r.add_argument("--stamp", required=True, help="备份目录名，如 20261013T060000Z")
    r.add_argument(
        "--confirm",
        required=True,
        help=f"必须为 {RESTORE_CONFIRM}，否则拒绝恢复",
    )
    r.add_argument("--artifacts", default="")
    r.add_argument("--backup-dir", default="")
    r.set_defaults(func=_cmd_restore)

    l = sub.add_parser("list", help="列出已有备份时间戳")
    l.add_argument("--backup-dir", default="")
    l.set_defaults(func=_cmd_list)

    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
