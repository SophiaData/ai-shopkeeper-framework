"""管理后台账号 CLI。

用法：
  PYTHONPATH=src python3 -m shopkeeper.workflow.admin_cli create-user \\
      --username owner --password '至少八位' --role owner

密码不会回显进日志；请勿把真实密码写进脚本或 Git。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from shopkeeper.workflow.auth import ROLES, create_admin_user
from shopkeeper.workflow.envfile import load_env_file
from shopkeeper.workflow.store import WorkflowStore


def _db_path() -> Path:
    return Path(os.getenv("WORKFLOW_DB_PATH", "artifacts/workflow.db"))


def cmd_create_user(args: argparse.Namespace) -> int:
    password = args.password
    if not password:
        password = getpass.getpass("密码（至少 8 位）: ")
        confirm = getpass.getpass("再输入一次: ")
        if password != confirm:
            print("两次密码不一致。", file=sys.stderr)
            return 2
    store = WorkflowStore(_db_path())
    try:
        user = create_admin_user(
            store,
            username=args.username,
            password=password,
            role=args.role,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"已创建管理员：{user.username}（角色 {user.role}，id={user.id}）")
    print(f"数据库：{_db_path()}")
    print("打开 http://127.0.0.1:8000/admin 登录。")
    return 0


def cmd_list_users(_args: argparse.Namespace) -> int:
    store = WorkflowStore(_db_path())
    rows = store.list_admin_users()
    if not rows:
        print("尚无管理员账号。请先 create-user。")
        return 0
    for row in rows:
        active = "active" if row.get("is_active") else "disabled"
        print(f"{row['username']}\t{row['role']}\t{active}\t{row['id']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="AI 店长管理后台账号工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create-user", help="创建本地管理员账号")
    create.add_argument("--username", required=True)
    create.add_argument("--password", default="", help="也可省略后交互输入")
    create.add_argument("--role", default="owner", choices=sorted(ROLES))
    create.set_defaults(func=cmd_create_user)

    listed = sub.add_parser("list-users", help="列出本地管理员")
    listed.set_defaults(func=cmd_list_users)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
