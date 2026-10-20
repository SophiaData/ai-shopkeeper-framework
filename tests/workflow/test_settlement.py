"""P2-10 结算单导入与对账（SCHEMA §4.7 第四条线 `ledger_vs_settlement`）。

## 关于 CSV 列名的重要说明

**本仓库无法核对 TikTok 结算单的真实导出列名**（文档页是 JS 登录墙、web 检索不可用）。
按 AGENTS 规则 4，我没有臆造官方表头，而是做**宽容映射**：

- 多个候选列名，忽略大小写与分隔符差异
- 匹配不到必需列时**报错并列出实际表头**
- 金额容忍 `12.34` / `1234` / `฿12.34` / `1,234.56` / `-12.34`

用例覆盖这些容错路径 —— 因为真实表头与候选不同时，用户只能靠报错信息自救。
"""

from __future__ import annotations

import pytest

from shopkeeper.workflow.settlement import (
    RECON_LINE,
    SettlementImportError,
    import_settlement_csv,
    parse_amount_minor,
    reconcile_against_ledger,
)
from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


# --------------------------------------------------------------------------
# 金额解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.34", 1234),
        ("1234", 123400),
        ("฿12.34", 1234),
        ("1,234.56", 123456),
        ("-12.34", -1234),  # 退款/罚款可以是负的
        ("0", 0),
        ("0.00", 0),
        (" 99.5 ", 9950),
        ("THB 50", 5000),
    ],
)
def test_amount_parsing(raw, expected):
    assert parse_amount_minor(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "฿", "  "])
def test_unparseable_amount_is_none_not_zero(raw):
    """**不填 0** —— 0 是「结算了 0 元」，与「没数据」是两件事。"""
    assert parse_amount_minor(raw) is None


# --------------------------------------------------------------------------
# 表头映射
# --------------------------------------------------------------------------


def test_english_header():
    r = import_settlement_csv("Order ID,Settlement Amount\nA,10.00\n")
    assert r.lines[0].order_id == "A"
    assert r.lines[0].amount_minor == 1000


def test_snake_case_header():
    r = import_settlement_csv("order_id,settlement_amount\nA,10.00\n")
    assert r.lines[0].amount_minor == 1000


def test_chinese_header():
    r = import_settlement_csv("订单号,结算金额\nA,50\n")
    assert r.lines[0].order_id == "A"
    assert r.lines[0].amount_minor == 5000


def test_header_with_suffix_matches():
    """`结算金额(THB)` 这种带后缀的真实表头也要认得。"""
    r = import_settlement_csv("订单号,结算金额(THB)\nA,50\n")
    assert r.lines[0].amount_minor == 5000


def test_currency_column_optional():
    r = import_settlement_csv("Order ID,Settlement Amount\nA,1.00\n")
    assert r.lines[0].currency == ""


def test_currency_column_read_when_present():
    r = import_settlement_csv("Order ID,Settlement Amount,Currency\nA,1.00,THB\n")
    assert r.lines[0].currency == "THB"


def test_missing_required_columns_lists_actual_header():
    """报错必须**列出实际表头** —— 否则用户只能对着「导入失败」猜。"""
    with pytest.raises(SettlementImportError) as exc:
        import_settlement_csv("foo,bar\n1,2\n")
    message = str(exc.value)
    assert "订单号" in message and "结算金额" in message
    assert "foo" in message and "bar" in message


def test_empty_input_rejected():
    with pytest.raises(SettlementImportError):
        import_settlement_csv("")


def test_headerless_input_rejected():
    with pytest.raises(SettlementImportError):
        import_settlement_csv("\n\n")


def test_rows_without_order_id_skipped():
    r = import_settlement_csv("Order ID,Settlement Amount\n,10.00\nA,5.00\n")
    assert r.skipped == 1
    assert [x.order_id for x in r.lines] == ["A"]


def test_unparseable_amount_row_skipped_with_warning():
    r = import_settlement_csv("Order ID,Settlement Amount\nA,abc\nB,5.00\n")
    assert r.skipped == 1
    assert len(r.lines) == 1
    assert any("A" in w for w in r.warnings)


def test_bom_tolerated():
    r = import_settlement_csv("\ufeffOrder ID,Settlement Amount\nA,1.00\n")
    assert r.lines[0].order_id == "A"


# --------------------------------------------------------------------------
# 对账
# --------------------------------------------------------------------------


def test_matching_amount_records_no_diff(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nA,10.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1000})
    assert out["matched"] == 1
    assert out["diff_recorded"] == 0
    assert store.list_recon_diffs() == []


def test_mismatch_is_critical_diff(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nA,50.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 4000})

    assert out["diff_recorded"] == 1
    diff = store.list_recon_diffs()[0]
    assert diff["recon_line"] == RECON_LINE
    assert diff["expected"] == 4000
    assert diff["actual"] == 5000
    assert diff["diff_amount"] == 1000
    assert diff["severity"] == "critical", "金额不符可能是费率/退款/汇率算错，不该只提示"


def test_order_missing_in_ledger_is_warn(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nNEW,10.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={})

    assert out["missing_in_ledger"] == ["NEW"]
    diff = store.list_recon_diffs()[0]
    assert diff["expected"] is None
    assert diff["severity"] == "warn"


def test_order_missing_in_settlement_reported(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nA,10.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1000, "B": 500})
    assert out["missing_in_settlement"] == ["B"]


def test_reconcile_does_not_touch_ledger(store):
    """对账**只记录**差异；修正须走 `ledger_correction` 审批（SCHEMA §4.6）。"""
    before = len(store.list_ledger_entries())
    r = import_settlement_csv("Order ID,Settlement Amount\nA,50.00\n")
    reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 4000})
    assert len(store.list_ledger_entries()) == before


def test_reconcile_summary_shape(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nA,10.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1000})
    for key in (
        "recon_line",
        "matched",
        "diff_recorded",
        "missing_in_ledger",
        "missing_in_settlement",
        "total_lines",
        "note",
    ):
        assert key in out
    assert "不自动冲正" in out["note"]


def test_shop_id_passed_through(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nA,50.00\n")
    reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 4000}, shop_id="th1")
    assert store.list_recon_diffs()[0]["shop_id"] == "th1"


# --------------------------------------------------------------------------
# 假列头：毛额列不能被当成结算净额（独立复核发现的缺陷）
# --------------------------------------------------------------------------


def test_gross_amount_column_is_rejected():
    """`Order Amount` 是**订单毛额（GMV）**，不是结算净额。

    把它当结算金额会**静默算错** —— 比报错更危险，因为对账表里会出现
    一个看起来很正常的数字，把真账差掩盖掉。必须报错而不是猜。
    """
    csv_text = "Order ID,Order Amount,Shipping Fee Amount,Currency\n576123,250.00,40.00,THB\n"
    with pytest.raises(SettlementImportError) as exc:
        import_settlement_csv(csv_text)
    assert "结算金额" in str(exc.value)


@pytest.mark.parametrize(
    "header",
    [
        "Order ID,Order Amount",
        "Order ID,Item Amount",
        "Order ID,Total Amount",
        "Order ID,GMV Amount",
        "订单号,订单金额",
    ],
)
def test_various_gross_columns_rejected(header):
    with pytest.raises(SettlementImportError):
        import_settlement_csv(header + "\nA,100\n")


def test_explicit_settlement_column_still_accepted():
    r = import_settlement_csv("Order ID,Settlement Amount\nA,10.00\n")
    assert r.lines[0].amount_minor == 1000


@pytest.mark.parametrize(
    "header",
    [
        "Order ID,Settlement Amount (THB)",
        "Order ID,Payout Amount",
        "Order ID,Net Amount",
        "订单号,结算金额(THB)",
        "订单号,结算净额",
    ],
)
def test_settlement_column_variants_accepted(header):
    """真实的表头变体要能认出来（否则只能靠报错自救）。"""
    r = import_settlement_csv(header + "\nA,10.00\n")
    assert r.lines[0].amount_minor == 1000


def test_ambiguous_amount_columns_raise():
    """两个容错候选同时匹配时**不猜**，明确报歧义。"""
    csv_text = "Order ID,Settlement Amount (THB),Payout Amount (THB)\nA,1.00,2.00\n"
    with pytest.raises(SettlementImportError) as exc:
        import_settlement_csv(csv_text)
    assert "多个列" in str(exc.value)


def test_exact_match_wins_over_fuzzy():
    """精确列名存在时直接用，不因另有相似列就报歧义。"""
    r = import_settlement_csv("Order ID,Settlement Amount,Payout Amount\nA,10.00,99.00\n")
    assert r.lines[0].amount_minor == 1000


# --------------------------------------------------------------------------
# 重复订单号（一个订单多行）—— 真实结算单常按「订单行」导出
# --------------------------------------------------------------------------


def test_duplicate_order_rows_are_summed(store):
    """**一个订单多个 SKU 就是多行** —— 必须合计后再比，不能逐行比。

    逐行比会把「合计正确」误报成 critical 差异，让卖家去追不存在的账差，
    而真正的账差反而被噪声淹没。
    """
    r = import_settlement_csv("Order ID,Settlement Amount\nA,5.00\nA,5.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1000})

    assert out["matched"] == 1, "两行各 5.00 合计 10.00，与台账 1000 分一致，应算匹配"
    assert out["diff_recorded"] == 0, "合计正确时不该报差异"
    assert store.list_recon_diffs() == []


def test_duplicate_order_rows_still_detect_real_mismatch(store):
    """聚合不能把真差异一起吞掉。"""
    r = import_settlement_csv("Order ID,Settlement Amount\nA,5.00\nA,6.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1000})

    assert out["diff_recorded"] == 1
    diff = store.list_recon_diffs()[0]
    assert diff["actual"] == 1100, "应比的是合计 11.00"
    assert diff["diff_amount"] == 100
    assert diff["severity"] == "critical"


def test_three_rows_sum_correctly(store):
    r = import_settlement_csv("Order ID,Settlement Amount\nA,1.00\nA,2.00\nA,3.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 600})
    assert out["matched"] == 1
    assert store.list_recon_diffs() == []


def test_each_order_reported_once(store):
    """同一订单多行时，**只应产生一条对账记录**（不是每行一条）。"""
    r = import_settlement_csv("Order ID,Settlement Amount\nA,5.00\nA,5.00\nA,5.00\n")
    reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1000})
    assert len(store.list_recon_diffs()) == 1, "三行同一订单，差异只该记一条"


def test_missing_in_ledger_not_duplicated(store):
    """多个新订单缺台账时，每个订单各报一次，不因多行重复。"""
    r = import_settlement_csv("Order ID,Settlement Amount\nNEW,1.00\nNEW,2.00\nOTHER,3.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={})
    assert sorted(out["missing_in_ledger"]) == ["NEW", "OTHER"]
    assert len(store.list_recon_diffs()) == 2


def test_negative_and_positive_rows_net_out(store):
    """退款（负数行）与正向结算应抵减 —— 这是聚合的另一个好处。"""
    r = import_settlement_csv("Order ID,Settlement Amount\nA,20.00\nA,-5.00\n")
    out = reconcile_against_ledger(store, r.lines, ledger_amounts={"A": 1500})
    assert out["matched"] == 1
    assert store.list_recon_diffs() == []


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_imports_and_reports(tmp_path, capsys, monkeypatch):
    import argparse

    from shopkeeper.cli import cmd_settlement_import

    csv_path = tmp_path / "settle.csv"
    csv_path.write_text("Order ID,Settlement Amount\nA,10.00\n", encoding="utf-8")

    rc = cmd_settlement_import(
        argparse.Namespace(settlement_in=str(csv_path), db=str(tmp_path / "w.db"), shop_id="")
    )

    assert rc == 0
    assert "ledger_vs_settlement" in capsys.readouterr().out


def _seed_ledger(store, order_id="A"):
    """按 SCHEMA §4.3 R1/R3 造一笔完整台账：accrual → 精确冲正 → 实付重记 → 费用。"""
    def add(key, etype, amount, basis, rev_of=None):
        store.insert_ledger_entry(
            {
                "shop_id": "s1",
                "entry_key": key,
                "order_id": order_id,
                "entry_type": etype,
                "currency": "THB",
                "amount": amount,
                "base_currency": "CNY",
                "base_amount": amount,
                "basis": basis,
                "reversal_of": rev_of,
                "ref_id": order_id,
            }
        )

    add("k1", "sale", 100000, "accrual")
    add("k2", "sale", -100000, "accrual", rev_of="k1")
    add("k3", "sale", 90000, "settlement")
    add("k4", "platform_commission", -18000, "settlement")


def test_cli_ledger_excludes_fees(tmp_path, capsys):
    """CLI 的「应结算额」必须是**实付重记**，不能把佣金等费用减进去。

    我第一版按 `basis="settlement"` + `ref_id` 累加**所有**分录，
    于是佣金（-18000）也被减掉，得到负数 → 每个有费用的订单都误报 critical。
    """
    import argparse

    from shopkeeper.cli import cmd_settlement_import
    from shopkeeper.workflow.store import WorkflowStore

    db = tmp_path / "w.db"
    _seed_ledger(WorkflowStore(db))
    csv_path = tmp_path / "s.csv"
    csv_path.write_text("Order ID,Settlement Amount\nA,900.00\n", encoding="utf-8")

    rc = cmd_settlement_import(
        argparse.Namespace(settlement_in=str(csv_path), db=str(db), shop_id="")
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert '"matched": 1' in out, "台账实付 90000 与结算单 900.00 一致，应匹配"
    assert '"diff_recorded": 0' in out, "不该报差异"


def test_cli_ledger_excludes_reversal_leg(tmp_path, capsys):
    """R1 精确冲正腿不是独立金额，重复计入会让应结算额翻倍。"""
    import argparse

    from shopkeeper.cli import cmd_settlement_import
    from shopkeeper.workflow.store import WorkflowStore

    db = tmp_path / "w.db"
    _seed_ledger(WorkflowStore(db))
    csv_path = tmp_path / "s.csv"
    csv_path.write_text("Order ID,Settlement Amount\nA,900.00\n", encoding="utf-8")

    cmd_settlement_import(argparse.Namespace(settlement_in=str(csv_path), db=str(db), shop_id=""))
    out = capsys.readouterr().out

    assert '"matched": 1' in out
    assert "expected" not in out, "匹配时不该产生任何差异记录"


def test_cli_detects_real_mismatch(tmp_path, capsys):
    """口径修好后仍要能抓到真差异。"""
    import argparse

    from shopkeeper.cli import cmd_settlement_import
    from shopkeeper.workflow.store import WorkflowStore

    db = tmp_path / "w.db"
    _seed_ledger(WorkflowStore(db))
    csv_path = tmp_path / "s.csv"
    csv_path.write_text("Order ID,Settlement Amount\nA,500.00\n", encoding="utf-8")

    cmd_settlement_import(argparse.Namespace(settlement_in=str(csv_path), db=str(db), shop_id=""))
    out = capsys.readouterr().out

    assert '"diff_recorded": 1' in out
    diff = WorkflowStore(db).list_recon_diffs()[0]
    assert diff["expected"] == 90000, "台账实付 90000"
    assert diff["actual"] == 50000, "平台只结算 50000"
    assert diff["diff_amount"] == -40000, "少结算 400 元（负号表示实际少于预期）"


def test_cli_with_empty_ledger_marks_missing(tmp_path, capsys):
    """完全没有台账分录时，应标「台账缺失」而不是给出一个貌似正确的匹配。"""
    import argparse

    from shopkeeper.cli import cmd_settlement_import

    csv_path = tmp_path / "s.csv"
    csv_path.write_text("Order ID,Settlement Amount\nA,10.00\n", encoding="utf-8")

    cmd_settlement_import(
        argparse.Namespace(settlement_in=str(csv_path), db=str(tmp_path / "w.db"), shop_id="")
    )
    out = capsys.readouterr().out

    assert '"missing_in_ledger"' in out
    assert '"matched": 0' in out


def test_cli_missing_file_returns_2(tmp_path):
    import argparse

    from shopkeeper.cli import cmd_settlement_import

    rc = cmd_settlement_import(
        argparse.Namespace(settlement_in=str(tmp_path / "nope.csv"), db="", shop_id="")
    )
    assert rc == 2


def test_cli_bad_header_returns_2_with_reason(tmp_path, capsys):
    import argparse

    from shopkeeper.cli import cmd_settlement_import

    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")

    rc = cmd_settlement_import(
        argparse.Namespace(settlement_in=str(bad), db=str(tmp_path / "w.db"), shop_id="")
    )
    assert rc == 2
    assert "订单号" in capsys.readouterr().err
