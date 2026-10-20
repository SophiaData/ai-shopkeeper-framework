# 阶段 2：M1 Demo 验收

> 给仓库所有者核对「贴一条 1688 链接 → 选品报告 → EN/TH/VI 文案和商品图」。纯生成，不发店铺。
> 自动门只证明交付目录里的文件在、而且非空。人工通过率 ≥ 80% 不能由本文代签，四列仍要人工填写（评分表不随本公开快照发布）。
> Done 条件的家是 [DESIGN.md](./DESIGN.md) §9。历史计时记录保留在私有开发仓。控制台 SSE 仍不做，见 [M1-PLAN.md](./M1-PLAN.md) §6。

## 1. 一键跑

在仓库根目录：

```bash
make demo
```

等价命令：

```bash
PYTHONPATH=src python3 -m shopkeeper.cli demo --mock --url "https://detail.1688.com/offer/600123456789.html" --out artifacts/demo
```

这条不抓 1688，也不访问 LLM 网关。链接只用来生成确定性夹具；终端会写明 URL 未被真实抓取。CI 跑的是同一条离线路径里的 pytest，不依赖外网。

标准输出里有一行 `墙钟:`，以及交付目录。退出码 0 表示 EN、TH、VI 各自至少有一张已复制进包、且非空的图。退出码 2 表示图没进包（处理失败，或只剩源图 URL）。不要把退出码 2 当成抽检通过。

交付目录是 `artifacts/demo/packages/listing_<id>/`：

| 路径 | 内容 |
|---|---|
| `report.json` | 选品报告（差价、竞争、合规风险） |
| `listing.json` | 三语 listing；`images_by_lang[].output_path` 是包内相对路径 |
| `copy/EN.txt`、`copy/TH.txt`、`copy/VI.txt` | 标题、卖点、描述 |
| `images/<语种>/01.png` | 商品图文件 |
| `index.md` | 上面这些路径、墙钟、以及「上架在卖家中心」 |

不带 `--mock` 时，文案走本机网关，图像走 Pillow。缺字体或网关失败时，图不会被填进包，进程以退出码 2 结束。那条不作为 CI 门。

## 2. 抽检

打开第 1 节的交付目录。

- `report.json` 里应能看到 `price_gap`、`competition`、`compliance_risk` 三节，而不是空对象。
- 三份 `copy/*.txt` 分别是英文、泰文、越南文，标题和卖点都在。
- `images/EN`、`images/TH`、`images/VI` 里各有一张非空 PNG。`--mock` 画的是按语种分色的色卡，上面是 ASCII 标签 `MOCK EN` / `MOCK TH` / `MOCK VI`，不是 1688 商品照片。色卡只证明文件进了包。要看真实照片的水印和叠字，需另跑不带 `--mock` 的 Pillow 链路，并在有字体的机器上目检。

人评四列（语种地道、事实准确、可上架、图可用）留空即视为未达成。mock 色卡不能打成「图可用：通过」。

## 3. 计时

`index.md` 和标准输出的墙钟是同一次进程的 monotonic，口径写在 `index.md`：从选品开始到交付包写完。门槛是 180 秒。离线这条应当远小于 3 分钟；它不能代替历史真实网关计时（那些记录保留在私有开发仓，页面抓取为回放）。

## 4. 上架

`make demo` 不调用平台写接口。`listing.json` 的 `delivery.publish` 为 false。上架、改价、发货、退款仍在 TikTok Shop 卖家中心由你本人操作。

## 5. 自动门

```bash
PYTHONPATH=src python3 -m pytest -m "not real"
```

`tests/stylist/test_demo_package.py` 用 mock 断言：报告、三份文案、三张图都在约定路径且非空；三张图的像素不相同；图像降级时退出码为 2，并且不会造出一张假图。
