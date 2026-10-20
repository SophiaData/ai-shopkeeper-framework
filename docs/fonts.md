# 图像链路内置字体

> 家：本文件说明 Pillow 图像适配器用的可再分发开源字体来源与许可。
> 代码入口：`src/shopkeeper/adapters/pillow_image.py`（`bundled_font_dir` / `DEFAULT_FONTS`）。
> 文件目录：`src/shopkeeper/assets/fonts/`。
> M2 真重绘：`ImageRedrawAdapter` / `RedrawAwareImageClient`（`src/shopkeeper/adapters/image_redraw.py`）默认关；开启后 mock 重绘用 Pillow 默认位图字，**不依赖**本目录 Noto。Pillow 后处理主链路与叠字仍用下列内置字体。

## 为什么内置

图像腿曾只查 `SYSTEMROOT/Fonts`（Windows）。macOS 与 Linux CI 没有 segoeui / tahoma / msyh，结果是 `IMG_FONT_MISSING`，`tests/adapters/test_pillow_image.py` 在 CI 上跳过 15 条（见 [M1-PLAN](./M1-PLAN.md) P1 与 [DESIGN §12.1](./DESIGN.md)）。跨平台要求产出一致，不能依赖本机已装某商业字体。

## 文件与用途

| 文件 | 覆盖 | 用途 |
|---|---|---|
| `NotoSans-Regular.ttf` | 拉丁文，含越南语变音符 | EN / VI 叠字 |
| `NotoSansThai-Regular.ttf` | 泰文 + 基本拉丁/数字 | TH 叠字（仍无 Raqm 时声调位置转人工，见适配器 `HUMAN_REVIEW_LANGS`）。文件由 Noto Sans Thai 与 Noto Sans 拉丁数字子集合并，避免标题里的 `316` 等 ASCII 变成豆腐 |
| `NotoSansSC-Subset.ttf` | 简体中文子集 | 仅 `mock://` 合成图上的中文卖点带；**不是**完整字库 |

系统字体（Windows Fonts、macOS `/Library/Fonts`、Linux `/usr/share/fonts`）仍作回退，查找顺序是内置优先。

## 来源与许可

三份字体均来自 Google Fonts / Noto 项目，许可证为 **SIL Open Font License 1.1**。完整文本在同目录：

- `OFL-NotoSans.txt` — [google/fonts ofl/notosans](https://github.com/google/fonts/tree/main/ofl/notosans)
- `OFL-NotoSansThai.txt` — [google/fonts ofl/notosansthai](https://github.com/google/fonts/tree/main/ofl/notosansthai)
- `OFL-NotoSansSC.txt` — [google/fonts ofl/notosanssc](https://github.com/google/fonts/tree/main/ofl/notosanssc)

二进制取自：

- `NotoSans-Regular.ttf`：`notofonts/noto-fonts` 的 `hinted/ttf/NotoSans/NotoSans-Regular.ttf`（经 jsDelivr 拉取，2026-10-12）
- `NotoSansThai-Regular.ttf`：同日拉取的 `NotoSansThai-Regular.ttf`，再与 Noto Sans 拉丁数字子集用 `fontTools.merge` 合并（OFL 允许修改再分发；许可文件仍为两份 OFL）
- `NotoSansSC-Subset.ttf`：由 `google/fonts` 的 `NotoSansSC[wght].ttf` 用 `pyftsubset` 裁出 mock 中文卖点带所需字形（长效保温小时不锈钢大容量一键开盖等），以控制仓库体积

OFL 允许嵌入与再分发；修改后的子集仍受 OFL 约束，保留上述许可文件。

## 验收

```bash
PYTHONPATH=src python3 -m pytest tests/adapters/test_pillow_image.py -m "not real" -q
PYTHONPATH=src python3 -m pytest tests/adapters/test_image_redraw.py -m "not real" -q
```

期望：Pillow 用例**不因缺字体而 skip**，且 TH/VI 的 `missing_glyphs` 为空、墨迹占比断言通过。真重绘用例在 `IMAGE_REDRAW_ENABLED=0` 走 Pillow、`=1` 走 mock redraw；水印 `not_found` 只写 warnings。
