"""历史密钥扫描的白名单只放行已核对的误报。

真命中（或白名单差一个字符）必须留下，并且报告里不能出现命中原文。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# 「必须被挡住」的夹具一律运行时拼接。
#
# 本文件会进 git 历史，`scripts/scan_history.py` 扫全历史时会把写在源码里的
# 完整字面量当成真命中（白名单只放行逐字核对过的值）。把字面量拆开拼接，
# 测试仍然断言同一个字符串，但 blob 里不再出现连续的高危形态。
# 白名单里的 `self_test_fixture_literals` 只兜住少数**规则本身要求**的样例。
def _sk(body: str) -> str:
    return "sk-" + body


def _phone(head: str, tail: str) -> str:
    return head + tail



def _json_field(name: str, value: str) -> str:
    """拼出 ``"<name>": "<value>"``，但源码里不出现连续的字段名+引号形态。

    ``seller_login`` / ``winport`` 两条正则都以「字段名 + 引号 + 值 + 引号」为形，
    把整段写在源码里会让本文件自己命中（连 ``%s`` 占位符也会被吃进去）。
    """
    q = chr(34)
    return q + name + q + ": " + q + value + q


def _winport_url(member_id: str) -> str:
    q = chr(34)
    return (
        "https://winport.m.1688.com/page/index.html?"
        + "memberId=" + member_id + "&offerId=896192389922"
    )


def _load():
    path = ROOT / "scripts" / "scan_history.py"
    spec = importlib.util.spec_from_file_location("scan_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在执行类体时会查 sys.modules[__module__]；先登记再 exec。
    sys.modules["scan_history"] = module
    spec.loader.exec_module(module)
    return module


scan = _load()


def _blocked(text: str, path: str = "notes.txt") -> list:
    hits = scan.find_hits(text, path=path, oid="abc")
    _ok, bad = scan.partition(hits)
    return bad


def _rule(text: str, path: str = "notes.txt") -> str | None:
    hits = scan.find_hits(text, path=path, oid="abc")
    assert len(hits) == 1, hits
    return scan.allow_rule_id(hits[0])


def test_allow_rule_ids_are_unique_and_explained():
    ids = [rule.id for rule in scan.ALLOW_RULES]
    assert len(ids) == len(set(ids))
    # 11 条按值匹配 + commit_object_sha_hex + self_test_fixture_literals。
    assert len(ids) == 13
    for rule in scan.ALLOW_RULES:
        assert len(rule.reason) >= 12


def test_llm_placeholder_is_allowed_and_real_key_is_not():
    assert _rule("SHOPKEEPER_LLM_API_KEY=local-gateway-no-auth\n") == "placeholder_llm_api_key"
    assert _blocked("SHOPKEEPER_LLM_API_KEY=" + _sk("notarealkey0123456789") + "\n")


def test_empty_api_key_does_not_swallow_a_same_line_value():
    text = "DIFY_API_KEY=\nWORKFLOW_LLM_BASE_URL=https://example.test\n"
    assert _rule(text) == "empty_api_key_next_env_name"
    # 同一行的值，即使长得像变量名，后面也不是「下一行的 =」。
    assert _blocked("API_KEY=WORKFLOW_LLM_BASE_URL\n")
    # 跨行，但吃进去的不是环境变量赋值。
    assert _blocked("API_KEY=\nnot_an_assignment_value\n")


def test_only_two_example_phones_are_allowed():
    assert _rule("call 13812345678 now") == "fixture_cn_mobile"
    assert _rule("call 13800138000 now") == "fixture_cn_mobile"
    assert _blocked("call " + _phone("139", "00001111") + " now")
    assert scan.find_hits("id 138123456789") == []


def test_cargo_checksum_is_allowed_only_inside_hex():
    # 中间那 11 位运行时拼进去：写死会让「本该被挡住」的样例进 blob。
    embedded = _phone("158", "12345678")
    digest = "d78c8dee4c7bf0e" + embedded + "fed6142ce9d3b85a408189d07482442145823b"
    line = f'checksum = "{digest}"\n'
    assert embedded in digest  # 夹在校验和里的 11 位，不是独立号码
    assert _rule(line, path="apps/desktop/src-tauri/Cargo.lock") == "cargo_lock_checksum_hex"
    assert _blocked('checksum = "' + embedded + '"\n', path="apps/desktop/src-tauri/Cargo.lock")
    assert _blocked(line, path="README.md")


def test_ibank_image_id_is_path_specific():
    text = (
        "https://cbu01.alicdn.com/img/ibank/" + _phone("158", "12345678") + "_558872943.jpg?whiteType=1"
    )
    path = "spike/alibaba_parse/_fc2.out"
    assert _rule(text, path=path) == "alicdn_ibank_image_id"
    assert _blocked("phone " + _phone("158", "12345678") + " in the same file", path=path)
    assert _blocked(text, path="docs/spike/1688-parse.md")


def test_redacted_seller_fields_only():
    ok = '"sellerLoginId": "<seller-id-redacted>"'
    old = '"sellerLoginId": "<seller-login-redacted>"'
    assert _rule(ok) == "redacted_seller_login"
    assert _rule(old) == "redacted_seller_login"
    demo_id = "shopkeeper_demo_001"
    assert _blocked(_json_field("sellerLoginId", demo_id))


def test_winport_rejects_raw_member_id():
    redacted = (
        '"winportUrl": "https://winport.m.1688.com/page/index.html?'
        'memberId=<seller-member-id-redacted>&offerId=896192389922"'
    )
    # memberId 运行时注入：完整 ``b2b-<8位以上>`` 不能进 blob。
    raw_id = "b2b-" + "2218547450375"
    raw = _json_field("winportUrl", _winport_url(raw_id))
    assert _rule(redacted) == "redacted_winport_ids"
    assert _blocked(raw)
    # 原始会员号本身还要被 seller_member_id 抓住，不能被 winport 规则带走。
    kinds = {hit.pattern for hit in _blocked(raw)}
    assert "seller_member_id" in kinds
    assert "winport" in kinds


def test_alphabet_sk_and_bearer_are_exact_shapes():
    full = "sk-" + "abcdefghijklmnopqrstuvwxyz"
    short = "sk-" + "abcdefghijklmnopqrstuvwxyz"[:22]
    hits = scan.find_hits(f"Authorization: Bearer {full}")
    rules = {hit.pattern: scan.allow_rule_id(hit) for hit in hits}
    assert rules == {
        "bearer": "alphabet_bearer_fixture",
        "sk_key": "alphabet_sk_fixture",
    }
    assert _rule(short) == "alphabet_sk_fixture"
    assert _blocked(_sk("abcdefghijklmnopqrstuvwxyz") + "1")
    assert _blocked("Bearer " + _sk("nottherealalphabetkey"))


def test_bearer_literals_do_not_cover_a_one_character_change():
    token = next(iter(scan._BEARER_LITERALS))
    assert _rule(f"Authorization: Bearer {token}") == "bearer_test_literals"
    last = "A" if not token.endswith("A") else "B"
    assert _blocked(f"Authorization: Bearer {token[:-1]}{last}")


def test_aws_example_key_only():
    assert _rule("AKIAIOSFODNN7EXAMPLE") == "aws_docs_example_key"
    assert _blocked("AKIAIOSFODNN7EXAMPLF")


def test_placeholder_does_not_hide_a_later_secret():
    text = "\n".join(
        [
            "SHOPKEEPER_LLM_API_KEY=local-gateway-no-auth",
            "Authorization: Bearer " + _sk("RealSecretKey0123456789"),
        ]
    )
    bad = _blocked(text)
    assert any(hit.pattern == "sk_key" for hit in bad)
    report = scan.format_report(
        scan.ScanResult(allowed=[], blocked=bad, objects=1, elapsed_s=0.01)
    )
    assert "RESULT: BLOCKED" in report
    assert _sk("RealSecretKey0123456789") not in report
    assert "local-gateway-no-auth" not in report


def test_report_of_allowed_hits_omits_the_value():
    text = "SHOPKEEPER_LLM_API_KEY=local-gateway-no-auth\n"
    hits = scan.find_hits(text, path=".env.example", oid="deadbeef")
    allowed, blocked = scan.partition(hits)
    assert blocked == []
    report = scan.format_report(
        scan.ScanResult(allowed=allowed, blocked=[], objects=1, elapsed_s=0.01)
    )
    assert "RESULT: CLEAN" in report
    assert "placeholder_llm_api_key" in report
    assert "local-gateway-no-auth" not in report


def test_budget_expiry_is_not_a_clean_result():
    result = scan.scan_git(budget_s=0)
    assert result.exit_code == 2
    report = scan.format_report(result)
    assert "RESULT: BUDGET" in report
    assert "RESULT: CLEAN" not in report


def test_tiny_budget_covers_object_enumeration():
    """预算必须从 rev-list 枚举阶段就开始生效。

    回归 ``scan_git`` 曾经只在 cat-file 读取循环里查 deadline：枚举阶段的
    ``git rev-list --objects --all`` 没有超时保护，极小预算下也会一直挂住。
    这里要求一个非零但极小的预算能在远小于常规扫描的时间内返回 BUDGET，
    而不是跑完整个枚举。
    """
    started = time.monotonic()
    result = scan.scan_git(budget_s=0.001)
    elapsed = time.monotonic() - started
    assert result.exit_code == 2
    assert result.error is not None
    assert elapsed < 10, f"极小预算下枚举未被截止，耗时 {elapsed:.1f}s"


def test_zero_budget_child_threads_do_not_outlive_the_call():
    """超时清理后不应留下仍在写管道的 feeder 线程。"""
    before = {t.ident for t in threading.enumerate()}
    scan.scan_git(budget_s=0.001)
    time.sleep(0.2)
    leaked = [
        t.name
        for t in threading.enumerate()
        if t.ident not in before and t.is_alive() and t is not threading.current_thread()
    ]
    assert leaked == [], f"超时清理留下了线程: {leaked}"


def test_full_history_has_no_unlisted_hit(monkeypatch):
    monkeypatch.chdir(ROOT)
    result = scan.scan_git(budget_s=60)
    assert result.error is None
    assert result.blocked == []
    assert result.allowed, "白名单没有命中，扫描可能没扫到历史"
    assert result.exit_code == 0
    report = scan.format_report(result)
    assert "RESULT: CLEAN" in report
    for hit, _rule_id in result.allowed:
        assert hit.value not in report


def test_cli_exits_zero_and_does_not_print_fixtures():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scan_history.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESULT: CLEAN" in completed.stdout
    for secret in (
        "local-gateway-no-auth",
        "AKIAIOSFODNN7EXAMPLE",
        "13812345678",
        "sk-abcdefghijklmnopqrstuvwxyz",
    ):
        assert secret not in completed.stdout
        assert secret not in completed.stderr


def test_history_job_keeps_the_three_required_names():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for required in (
        "name: pytest (not real)",
        "name: pytest (not real, windows)",
        "name: desktop scaffold (no rustc)",
    ):
        assert required in text
    assert "name: secret scan (history)" in text
    section = text.split("\n  secret-scan-history:", 1)[1]
    # 下一个同级 job 之前；本 job 在文件末尾时整段都算。
    nxt = re.search(r"\n  [a-z0-9-]+:\n", section)
    body = section[: nxt.start()] if nxt else section
    assert "fetch-depth: 0" in body
    assert "timeout-minutes: 3" in body
    assert "timeout-minutes: 2" in body
    assert "scripts/scan_history.py" in body
    assert "continue-on-error" not in body
    local = (ROOT / "scripts" / "ci.ps1").read_text(encoding="utf-8")
    assert "scan_history.py" in local


def _commit(text: str) -> list:
    """commit 对象：无 path，oid 是提交号。"""
    hits = scan.find_hits(text, path="", oid="a11b46d0cd01abf6bf924b3f38db0a4abefe5b77")
    _ok, bad = scan.partition(hits)
    return bad


def test_commit_tree_sha_hex_is_not_a_phone_number():
    # 实测：tree 行前 11 位数字撞上手机号正则。
    hex40 = _phone("159", "70623811") + "aa8c641ba4cd6f17f863e489ecf63"
    assert len(hex40) == 40
    text = f"tree {hex40}\nparent {'a' * 40}\n"
    assert _commit(text) == []


def test_commit_sha_rule_does_not_swallow_a_real_phone_number():
    # 同一提交对象里，号码出现在 commit message 里就不该被 SHA 规则放行。
    msg = "\n\n联系 " + _phone("159", "70623811") + " 处理工单\n"
    assert _commit(msg)


def test_commit_sha_rule_requires_forty_hex_and_commit_head():
    # 十一号串单独出现（不在 tree/parent 行）不放行。
    assert _commit("\n\n号码 " + _phone("159", "70623811") + "\n")
    # 带 path 的 blob 里即使像 SHA 也不放行（这条规则只认 commit 对象）。
    hex40 = _phone("159", "70623811") + "aa8c641ba4cd6f17f863e489ecf63"
    assert _blocked(f"tree {hex40}\n")


def test_self_test_fixture_rule_is_path_and_value_exact():
    path = "tests/security/test_scan_history_whitelist.py"
    literal = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "1"
    assert _rule(f'"{literal}"', path=path) == "self_test_fixture_literals"
    # 同一路径下没核对过的字面量仍被挡住。
    other = "sk-" + "Zz9" + "Q" * 20
    assert _blocked(f'"{other}"', path=path)
    # 同一个字面量出现在别的文件里也不放行。
    assert _blocked(f'"{literal}"', path="docs/notes.md")


def test_self_test_json_rule_only_covers_redacted_and_demo():
    path = "tests/security/test_scan_history_whitelist.py"
    redacted = '"winportUrl": "https://x.test/?memberId=<seller-member-id-redacted>"'
    # 既有规则先命中：红字占位本来就该由 redacted_winport_ids 放行。
    assert _rule(redacted, path=path) == "redacted_winport_ids"
    demo = '"sellerLoginId": "shopkeeper_demo_001"'
    assert _rule(demo, path=path) == "self_test_fixture_literals"
    # 真实形态的 id 不在放行之列。
    raw_id = "b2b-" + "9876543210"
    assert _blocked(_json_field("winportUrl", _winport_url(raw_id)), path=path)


@pytest.mark.parametrize(
    "sample",
    [
        "ghp_" + "a" * 36,
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "b2b-12345678",
    ],
)
def test_unrelated_secret_shapes_stay_blocked(sample: str):
    assert _blocked(sample)
