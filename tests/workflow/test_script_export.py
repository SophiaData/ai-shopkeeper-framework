"""P2-06 短视频脚本 → 素材包导出。

必须钉住的性质：
1. 导出产出 md / CSV / 封面 PNG 三样，且**真的能读**（不是空壳）
2. **不发布、不上传** —— 本仓库不代发短视频
3. 泰文进 CSV 后往返不坏（逗号/引号/换行同理）
4. 缺字体/缺 Pillow 时**降级而不是崩**，但要能看出降级了
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from shopkeeper.workflow.script_export import (
    SHOT_COLUMNS,
    export_script_package,
    render_cover_png,
    render_storyboard_csv,
    render_storyboard_markdown,
)


def _script(**over) -> dict:
    base = {
        "duration_s": 21,
        "cart": True,
        "scene": "出租屋收纳",
        "bgm_mood": "轻快",
        "shots": [
            {
                "seconds": f"{i * 4}-{i * 4 + 4}",
                "beat": f"beat{i}",
                "visual": f"画面{i}",
                "voice_th": f"เสียง{i}",
                "voice_zh": f"口播{i}",
                "subtitle_th": f"คำบรรยาย{i}",
                "note": "",
            }
            for i in range(5)
        ],
        "hooks": [{"voice_th": f"ฮุก{i}", "voice_zh": f"钩子{i}"} for i in range(3)],
        "covers": [
            {"text_th": "กล่องพับได้", "text_zh": "折叠箱"},
            {"text_th": "ประหยัดพื้นที่", "text_zh": "省空间"},
        ],
        "teleprompter_th": ["บรรทัดหนึ่ง", "บรรทัดสอง"],
        "shoot_checks": {"has_size_reference": True, "avoids_absolute_claims": True},
        "closing_note": "记得关购物车提醒。",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# 分镜 md
# --------------------------------------------------------------------------


def test_markdown_lists_every_shot():
    md = render_storyboard_markdown(_script(), title="折叠收纳箱")
    assert "# 短视频分镜：折叠收纳箱" in md
    for i in range(5):
        assert f"beat{i}" in md
        assert f"เสียง{i}" in md
        assert f"口播{i}" in md


def test_markdown_states_it_does_not_publish():
    """这是给拍摄用的文档，必须写明不发布，避免被当成发布流程。"""
    md = render_storyboard_markdown(_script())
    assert "不发布" in md


def test_markdown_includes_hooks_covers_teleprompter():
    md = render_storyboard_markdown(_script())
    assert "开场钩子" in md and "ฮุก0" in md
    assert "封面文案" in md and "กล่องพับได้" in md
    assert "提词器" in md and "บรรทัดหนึ่ง" in md


def test_markdown_shows_shoot_checks():
    md = render_storyboard_markdown(_script())
    assert "有尺寸参照物：✅" in md
    assert "无绝对化宣称：✅" in md


def test_markdown_marks_failed_checks():
    md = render_storyboard_markdown(
        _script(shoot_checks={"has_size_reference": False, "avoids_absolute_claims": False})
    )
    assert "有尺寸参照物：❌" in md


def test_markdown_survives_missing_fields():
    """脚本残缺时不该抛错 —— 导出一份能看的东西比崩掉有用。"""
    md = render_storyboard_markdown({})
    assert "短视频分镜" in md


def test_markdown_survives_garbage_shots():
    md = render_storyboard_markdown({"shots": ["不是对象", None, {"beat": "ok"}]})
    assert "ok" in md


# --------------------------------------------------------------------------
# 分镜 CSV
# --------------------------------------------------------------------------


def test_csv_header_and_row_count():
    rows = list(csv.DictReader(io.StringIO(render_storyboard_csv(_script()))))
    assert len(rows) == 5
    assert list(rows[0].keys()) == list(SHOT_COLUMNS)


def test_csv_thai_roundtrip():
    rows = list(csv.DictReader(io.StringIO(render_storyboard_csv(_script()))))
    assert rows[2]["voice_th"] == "เสียง2"
    assert rows[2]["subtitle_th"] == "คำบรรยาย2"


def test_csv_escapes_commas_quotes_newlines():
    script = _script()
    script["shots"][0]["note"] = '含,逗号 "引号" 和\n换行'
    rows = list(csv.DictReader(io.StringIO(render_storyboard_csv(script))))
    assert rows[0]["note"] == '含,逗号 "引号" 和\n换行'


def test_csv_crlf_for_excel():
    assert "\r\n" in render_storyboard_csv(_script())


def test_csv_shot_no_is_one_based():
    rows = list(csv.DictReader(io.StringIO(render_storyboard_csv(_script()))))
    assert [r["shot_no"] for r in rows] == ["1", "2", "3", "4", "5"]


# --------------------------------------------------------------------------
# 封面图
# --------------------------------------------------------------------------


def test_cover_png_is_a_real_png():
    payload = render_cover_png("กล่องพับได้")
    assert payload is not None, "Pillow 不可用"
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(payload) > 1000


def test_cover_png_handles_empty_text():
    """空文案也要出图，别抛错。"""
    payload = render_cover_png("")
    assert payload is not None
    assert payload[:4] == b"\x89PNG"


def test_cover_png_handles_very_long_text():
    payload = render_cover_png(" długi" * 200)
    assert payload is not None


# --------------------------------------------------------------------------
# 端到端：写盘
# --------------------------------------------------------------------------


def test_export_writes_all_artifacts(tmp_path):
    result = export_script_package(_script(), tmp_path / "pack", title="收纳箱")

    assert (tmp_path / "pack" / "storyboard.md").is_file()
    assert (tmp_path / "pack" / "storyboard.csv").is_file()
    assert result["shot_count"] == 5
    assert set(result["files"]) == {"storyboard.md", "storyboard.csv", "cover_1.png", "cover_2.png"}


def test_export_never_publishes(tmp_path):
    """核心约束：导出绝不发布、绝不上传。"""
    result = export_script_package(_script(), tmp_path / "pack")
    assert result["published"] is False
    assert result["uploaded"] is False
    assert "不发布" in result["note"] or "不上传" in result["note"]


def test_export_creates_out_dir(tmp_path):
    target = tmp_path / "deep" / "nested" / "pack"
    export_script_package(_script(), target)
    assert target.is_dir()


def test_export_with_no_covers(tmp_path):
    result = export_script_package(_script(covers=[]), tmp_path / "pack")
    assert result["covers"] == []
    assert "storyboard.md" in result["files"]


def test_export_is_repeatable(tmp_path):
    """重复导出应覆盖同名文件，不产生副本堆积。"""
    out = tmp_path / "pack"
    first = export_script_package(_script(), out)
    second = export_script_package(_script(), out)
    assert first["files"] == second["files"]
    assert len(list(out.iterdir())) == len(first["files"])


# --------------------------------------------------------------------------
# 端到端：真实脚本结构（不是手搓 fixture）
# --------------------------------------------------------------------------


def test_export_real_deterministic_script(tmp_path):
    """用 `build_deterministic_script` 的真实产出跑一遍 —— 确保字段名对得上。"""
    from shopkeeper.orchestrator.scripting import (
        build_deterministic_script,
        listing_script_source,
    )

    source = listing_script_source(
        listing_copy={"title_th": "กล่องเก็บของพับได้", "title_en": "Foldable Box"},
        facts={"title_raw": "折叠收纳箱", "size_note": "40x30x25cm"},
        run_input={"selling_points": ["折叠省空间", "PP 材质"], "duration_s": 21, "cart": True},
    )
    script = build_deterministic_script(source)

    result = export_script_package(script, tmp_path / "pack", title="折叠收纳箱")

    assert result["shot_count"] == 5, "真实脚本应有 5 个分镜"
    assert (tmp_path / "pack" / "cover_1.png").is_file()
    md = (tmp_path / "pack" / "storyboard.md").read_text(encoding="utf-8")
    assert "泰语口播" in md
    assert md.count("###") == 5, "五个分镜标题"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_exports_and_reports(tmp_path, capsys):
    import argparse

    from shopkeeper.cli import cmd_script_export

    src = tmp_path / "script.json"
    src.write_text(json.dumps(_script(), ensure_ascii=False), encoding="utf-8")

    rc = cmd_script_export(
        argparse.Namespace(script_in=str(src), out=str(tmp_path / "pack"), title="t")
    )

    assert rc == 0
    assert (tmp_path / "pack" / "storyboard.md").is_file()
    assert "published" in capsys.readouterr().out


def test_cli_missing_file_returns_2(tmp_path):
    import argparse

    from shopkeeper.cli import cmd_script_export

    rc = cmd_script_export(
        argparse.Namespace(script_in=str(tmp_path / "nope.json"), out=str(tmp_path / "o"), title="")
    )
    assert rc == 2


def test_cli_invalid_json_returns_2(tmp_path):
    import argparse

    from shopkeeper.cli import cmd_script_export

    bad = tmp_path / "bad.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    rc = cmd_script_export(
        argparse.Namespace(script_in=str(bad), out=str(tmp_path / "o"), title="")
    )
    assert rc == 2


def test_cli_non_object_json_returns_2(tmp_path):
    import argparse

    from shopkeeper.cli import cmd_script_export

    arr = tmp_path / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    rc = cmd_script_export(
        argparse.Namespace(script_in=str(arr), out=str(tmp_path / "o"), title="")
    )
    assert rc == 2


# --------------------------------------------------------------------------
# 非 dict 元素过滤（此前 hooks/covers 会让整条链路崩掉）
# --------------------------------------------------------------------------


def test_non_dict_cover_does_not_crash():
    """`{"covers": ["纯字符串"]}` 不该抛 `AttributeError`。

    脚本 JSON 由 LLM 或人手生成，混进一个字符串是**现实**的输入。
    `shots` 一直有过滤，但 `hooks`/`covers` 只看容器是不是 list ——
    元素非 dict 时 `.get()` 直接崩，CLI 层没包这层，用户看到 Python traceback。
    """
    script = {"shots": [{"beat": "a"}], "covers": ["纯字符串"]}
    md = render_storyboard_markdown(script)
    assert isinstance(md, str)


def test_non_dict_hook_does_not_crash():
    script = {"shots": [{"beat": "a"}], "hooks": ["纯字符串"]}
    assert isinstance(render_storyboard_markdown(script), str)


def test_mixed_types_in_hooks_and_covers(tmp_path):
    """数字、None、空 dict 混在里面也要稳。"""
    script = {
        "shots": [{"beat": "a"}],
        "hooks": ["x", 1, None, {"voice_th": "สวัสดี"}],
        "covers": ["y", {}, {"text_th": "ทดสอบ"}],
    }
    md = render_storyboard_markdown(script)
    assert "สวัสดี" in md, "合法的 hook 仍要渲染出来"
    assert isinstance(export_script_package(script, tmp_path), dict)


# --------------------------------------------------------------------------
# 空白封面必须被标出来
# --------------------------------------------------------------------------


def test_blank_cover_is_reported(tmp_path, monkeypatch):
    """缺字体时仍出图，但**必须让调用方知道标题没画上**。

    原先 `render_cover_png` 只返回 `bytes`，于是一张纯黑 1080×1920
    会被当成可用封面交给卖家 —— 而本函数的 docstring 自己写着
    「静默产出一张空白封面比报错更糟」。
    """
    import shopkeeper.workflow.script_export as se

    monkeypatch.setattr(se, "_load_font", lambda *a, **k: None)
    script = {"shots": [{"beat": "a"}], "covers": [{"text_th": "ทดสอบ"}]}
    result = se.export_script_package(script, tmp_path)

    assert result["covers"] == ["cover_1.png"], "底图仍产出，便于人工补字"
    assert result["covers_without_title"] == ["cover_1.png"], "必须标出来"
    assert "没有画上标题" in result["note"]


def test_good_cover_is_not_flagged(tmp_path):
    """正常情况不该误报 —— 否则提示会变成噪声。"""
    script = {"shots": [{"beat": "a"}], "covers": [{"text_th": "ทดสอบ"}]}
    result = export_script_package(script, tmp_path)
    assert result["covers"] == ["cover_1.png"]
    assert result["covers_without_title"] == []


def test_render_cover_png_status_flag():
    """`with_status=True` 时返回 `(payload, drew_text)`。"""
    from shopkeeper.workflow.script_export import render_cover_png

    out = render_cover_png("ทดสอบ", with_status=True)
    assert out is not None
    payload, drew_text = out
    assert isinstance(payload, bytes) and payload[:4] == b"\x89PNG"
    assert isinstance(drew_text, bool)


def test_empty_text_cover_is_flagged(tmp_path):
    """文案为空时也算「没画上标题」。"""
    script = {"shots": [{"beat": "a"}], "covers": [{"text_th": ""}]}
    result = export_script_package(script, tmp_path)
    assert result["covers_without_title"] == ["cover_1.png"]
