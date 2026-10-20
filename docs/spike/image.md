# Spike 报告：图像通道技术验证（M1-A / task-3）

> 运行时间：2026-10-10 18:22–18:31（Asia/Shanghai，本机 Windows / Python 3.12.2 / Pillow 12.3.0）
> 脚本与原始结果保留在本地探针目录 `spike/image/`（不随公开快照发布）。
> 对应设计：DESIGN.md §8「图像」行（通义万相/即梦 + 本地 Pillow 后处理）、§9 M1-A 第 3 项、§7.3 第 3 条、§9 M1-B 失败降级

---

## 1. 通道裁决表（全部为当次真实运行输出）

| # | 通道 | 请求 | HTTP | 裁决 | 原始报错/证据（响应体原文） |
|---|------|------|------|------|------|
| A1 | magpie 网关 `GET /v1/models` | 无鉴权 | 200 | 可用（但**列表不含任何图像模型**） | 37 个模型全部为文本 LLM；`workbuddy/hunyuan-image-v3.0-art` 不在列表中，**却能用**（见 A2）→ 网关有未列出的隐藏图像路由 |
| A2 | magpie 网关 `POST /v1/images/generations`，model=`workbuddy/hunyuan-image-v3.0-art`（客户端 settings 的 imageGen 配置） | OpenAI 图像协议 | **200 ×5/5** | **可用（文生图）** | 每次返回 `data[0].url`（腾讯云 COS 签名 URL）+ `usage` token 计数；PNG 已下载验证为真实 1024×1024 图像，见 §2 |
| A3 | 同端点，model=`workbuddy/space-bunny`（文本模型） | 同上 | 400 | 不支持该模型 | `"workbuddy: 400 Bad Request: Create image failed with error: Image model [space-bunny] route config not found (14401)"` |
| A3' | 同端点，model=`nonexistent/model-xyz` | 同上 | 404 | 未知模型 | `"magpie knows no model \"nonexistent/model-xyz\" to draw with"` |
| A5a | `POST /v1/images/edits`（multipart，源图+prompt 去水印） | 图生图 | 400 | **不支持图生图** | `"WorkBuddy draws hunyuan-image-v3.0-art from a prompt only: its config lists no model to edit images with"` |
| A5b | generations 带 `image` 参数（data URL） | 图生图 | 400 | **不支持图生图** | 同 A5a 原文（data URL 与截断 base64 两次探测均 400） |
| A5c | `POST /v1/images/variations` | 变体 | 404 | 端点不存在 | `"magpie serves /v1/chat/completions, /v1/responses, /v1/messages, /v1/systemone, /v1/images/generations, /v1/images/edits, /v1/videos, /v1/embeddings, /v1/rerank and /v1beta/models/*"` |
| B1 | DashScope 原生 `POST /api/v1/services/aigc/text2image/image-synthesis`（无 key） | 通义万相 | 401 | **需 key**（服务可达，协议在） | `{"code":"InvalidApiKey","message":"No API-key provided.","request_id":"c04bf71d-70bf-9cfe-860a-5605e840be53"}` |
| B2 | DashScope `POST /compatible-mode/v1/images/generations`（无 key） | OpenAI 兼容 | 404 | 该路径不存在（DashScope 走原生协议，非 OpenAI images 协议） | 响应体为空，状态码 404 |
| B3 | `GET https://dashscope.aliyuncs.com/` | 可达性 | 404 | 网络可达（404 属正常） | — |
| C | 即梦（Jimeng）API | — | — | **未探测**（无稳定公开端点/文档入口，且 A 通道已覆盖网关侧图像能力；见 §7「刻意未验」） | — |

**网关图像端点结论：通。** `http://127.0.0.1:3425/v1/images/generations` 对 `workbuddy/hunyuan-image-v3.0-art` 是 OpenAI 协议、无鉴权、真实出图（5 次全 200，图片已落盘）。但它是**纯文生图**：edits / image 参数 / variations 全部拒绝，**无法把 1688 源图喂进去做"保外观重绘"**。

## 2. 真实产出证据（本机文件，非描述）

网关生成（`spike/image/out/`）：
- `gw_hunyuan_apple.png`（1024×1024 PNG，~1.4MB，肉眼验证为真实商品级红苹果图）
- `gw_hunyuan_text_en.png` —— 关键测试：prompt 要求图上渲染英文 "STAY WARM" + "316 Steel · 500ml" → **字形完全正确**
- `gw_hunyuan_text_th.png` —— prompt 要求渲染泰语 "เก็บความร้อนได้นาน" → **泰语声调符/元音位置完全正确**（模型侧排版无问题）。⚠️ 但模型在杯身**幻觉出品牌字 "NONA"** —— 文生图会自造品牌文本，这是新的合规风险点
- `gw_hunyuan_768.png`、`gw_hunyuan_b64.png`（size 参数与 b64_json 返回格式均支持）

本地 Pillow 链路（输入为**自造测试图** `00_source_test.png`：白底+几何保温杯剪影+中文卖点带+右下角假 1688 红角标；非真实商品图）：
- `10_processed_EN.png` / `10_processed_TH.png` / `10_processed_VI.png` —— 完整链路产出：等比缩放白底补齐 → 水印遮盖 → 中文带整行抹除 → 目标语言文案叠加 → 残留检查 PASS → PNG tEXt 打标
- `01_source_normalized.png`（仅归一化对照）、`02_watermark_crop.png`（裁剪模式对照）
- `05_font_render_compare.png`（泰语 Tahoma vs Segoe UI 豆腐块对照）、`11_th_headline_zoom.png`、`12_vi_headline_zoom.png`（放大复检图）

## 3. 泰语/越南语字形实测（重点结论）

本机 Pillow 12.3.0：**FreeType2 有，Raqm/HarfBuzz 排版引擎无**（`features.check("raqm") == False`，不可 pip 安装）。逐字体字形覆盖实测（豆腐块判定 = 与私有区 U+E005 字形位图全等）：

| 字体 | EN | TH | VI | CJK |
|------|----|----|----|-----|
| Tahoma | ✓ | **✓（本机唯一有泰语字形的字体）** | ✓ | 缺 |
| Segoe UI / Arial / Verdana / Calibri / Times | ✓ | 缺（实测渲染为整排豆腐块，见 `05_font_render_compare.png`） | ✓ | 缺 |
| Microsoft YaHei | ✓ | 缺 | **缺 ế ệ ỉ ờ ữ**（越南语也不完整！） | ✓ |
| SimHei / SimSun / MingLiU | ✓ | 缺 | 缺多个带音符字符 | ✓ |

- **越南语：本地叠加可行**。预组合拉丁字符不需要排版引擎，Tahoma/Segoe UI/Arial 覆盖完整，实测 `12_vi_headline_zoom.png` 渲染正确。
- **泰语：有条件可行**。Tahoma 有全部字形，测试串 "เก็บความร้อนได้นาน" 无 Raqm 时声调符/元音落位**目测可接受**（Tahoma 附零宽音符自带定位），但**无 HarfBuzz 合成保证**——多音符叠合（如 ำ+ไม้โท 组合、ื 与声调同现）可能错位/碰撞。裁决：**EN/VI 可全自动；TH 本地叠加必须进人工抽检，或给 Pillow 装 Raqm 后复测**。
- 缺字体清单（如实报告）：本机**未安装**任何专门泰语字体（Sahadeva、Leelawadee UI 均无），泰语只能靠 Tahoma；若目标站点含老挝语/高棉语/缅甸语，本机**零覆盖**（Fonts 目录无对应字体文件）。

## 4. 成本对比：真实 API 重绘 vs 本地 Pillow 后处理

| 维度 | 网关 hunyuan 文生图（"重绘"） | 本地 Pillow 后处理 |
|------|------|------|
| 单张耗时（实测） | **8.7–13.4 s**（5 样本，另有 8.0–12.2 s 首轮），下载再 +0.6–0.8 s | **96–113 ms**（含残留检查与模板匹配 ~80ms） |
| 单张费用 | **拿不到**：网关返回 `usage`（约 919–1503 tokens/张）但本机无计费/价格端点，无法折算金额 | ¥0 |
| 产品外观一致性 | **不保证**：纯文生图，产品由模型重新想象（实测保温杯形状每次不同，且幻觉品牌字 "NONA"）→ 无货源模式下**图文不符 = 平台违规风险** | 完全保留原图商品，只动水印/文字区 |
| 水印去除 | 天然无 1688 水印（全新图），但可能自造其他文字 | 定点遮盖/裁剪，位置可控 |
| 图内翻译 | 强：EN/TH 字形渲染实测完美（模型侧排版） | EN/VI 可靠；TH 有排版风险（§3） |
| 依赖 | 网关进程活着（本机 magpie，**非官方商用 API**，路由随时可撤） | 仅 Pillow，M1 必达 |

## 5. 水印残留检查（DESIGN §7.3 第 3 条「重绘流程内嵌检查」）

`local_pipeline.py` 已实现**可执行**的三项检查（纯 Pillow，零新依赖），实测输出：

1. **角区方差**：处理后水印区域 `stddev=0.0`（干净背景应 <6.0）
2. **dHash 距离**：处理前后同区域 17/64（应 ≥10，证明水印像素确实被改变）
3. **"1688" 模板匹配**：原图角区模板在成品上滑窗找最小均方差，MSS=5970.9（越大越无残留），耗时 ~80ms

判定 `PASS(无残留)`。**M1 能做到**：遮盖/裁剪路径的角标类水印（1688 角标是固定样式）可自动判定 + 失败转人工待办。**M1 做不到（如实说）**：
- 全图任意位置半透明文字水印 → 需要 OCR 复检（paddleocr/easyocr 未装且不许 pip install）→ 列为 M2 依赖；M1 用"方差+模板匹配+人工抽检"顶替
- 文生图路径的"检查"语义不同：不是查残留，而是查**幻觉文字**（实测 "NONA"）→ 同样需要 OCR 或人工看；M1 对生成图强制人工抽检
- 本次自测还发现一个真实教训：首版只按文字带矩形擦除，**溢出带外的中文字残留**（`10_processed` 首跑右缘可见）→ 修正为整行全宽擦除。说明"检查"必须跑在**处理之后**而不是信任处理框。

## 6. AI 生成内容打标（图像侧）

`local_pipeline.py` 的 `tag_ai_content()` 已落地两层：
- **PNG tEXt 元数据**：`ai_generated=true/false` + `pipeline=hunyuan-redraw|pillow-postprocess` + 时间戳（重绘产物标 true，后处理产物标 false——后处理是编辑不是生成，但两者都入库留痕）
- **可选可见角标** "AI Generated"（代码就绪，默认关，按平台要求开）

M1 边界：TikTok Shop 各站点对 AI 内容标识的具体字段/位置要求**未查证**（需卖家后台文档），M1 先保证内部留痕 + 元数据完整，平台字段映射留 M2。DESIGN §10 的"图像 API key"若最终走 DashScope 通义系，其产物本身带 C2PA/隐式标识政策，接入时再核。

## 7. 降级裁决（M1-B ImageClient 建议）

**建议：M1-B 主链路 = 本地 Pillow 后处理（§9 M1-B 已授权的降级形态转正），网关文生图仅作辅助，通义万相不作为 M1 依赖。** 理由：

1. 酷 demo 的硬要求是"源图 → 无水印 + 目标语言图"且**图必须是那个商品**（无货源模式图文不能不符）。当前唯一通的生成通道（A2）是纯文生图，**喂不进源图**（A5a/b/c 三条路全被原始报错封死），做不了"保外观重绘"——它生成的越好看，图文不符风险越大。
2. DESIGN §8 原定选型（通义万相/即梦）本机**无 key**，B1 只证明服务可达；把 M1-B 排期押在一个需申请凭据的依赖上违反 M1-A 时限原则。
3. Pillow 链路实测 100ms/张、¥0、EN/VI 全自动、TH 人工抽检兜底，且**残留检查已内嵌**（§5），恰好满足 §7.3 第 3 条的"流程内嵌检查"。
4. ImageClient 接口按三态实现：`postprocess_ok`（默认产出）/ `postprocess_check_failed → 保留原图 + 标记人工处理`（§9 M1-B 授权路径）/ `redraw_available`（网关文生图，仅用于场景图/氛围图等不承诺商品一致性的资产，产物强制 `ai_generated=true` + 人工抽检）。
5. 后续动作（不阻塞 M1-B）：申请 DashScope key 后复测 B1 的图像编辑/翻译重绘类模型（万相有 img2img 产品线，本机无法验证，见 §8）；若其支持"源图+指令"协议，M2 再切主链路。

## 8. 单张耗时与成本汇总

| 路径 | 耗时（实测） | 成本 |
|------|------|------|
| 网关文生图 1024×1024 | 8.7–13.4 s/张 + 下载 0.6–0.8 s | **拿不到**（有 usage token 计数 ~919–1503/张，无价格端点） |
| Pillow 后处理全链路 | 96–113 ms/张 | ¥0 |
| DashScope 通义万相 | 未验证（需 key） | 未验证（需 key） |

## 9. 三栏收尾

**已验证（有当次运行输出/落盘文件支撑）**
- magpie 网关 `/v1/images/generations` + `workbuddy/hunyuan-image-v3.0-art` 真实出图（5/5 HTTP 200，PNG 落盘并目检），含 EN/泰语图内文字渲染、size 参数、b64_json
- 该通道**不支持**任何图生图形态（edits/dataURL/variations 三条原始报错）
- 文本模型走 images 端点、未知模型的网关报错原文
- DashScope 可达 + 无 key 401 原文（`InvalidApiKey`）；compatible-mode 无 images 端点（404）
- Pillow 后处理全链路出图（EN/TH/VI 三张）、水印遮盖+残留三项检查 PASS、PNG 元数据打标
- 本机字体覆盖矩阵（泰语仅 Tahoma、雅黑连越南语都不完整、Raqm 缺失）及豆腐块对照图

**刻意未验（超 scope 或需凭据/依赖）**
- 通义万相/即梦的实际生成质量、耗时、价格（需 key，未申请；不猜）
- OCR 复检水印残留（paddleocr/easyocr 未装，不许 pip install）
- 真实 1688 商品图作为输入（属 task-2 采集范围，且本任务约定用自造测试图）
- TikTok Shop 平台侧 AI 标识/水印的具体字段要求（运营文档问题，非本机可测）
- 网关图像路由的稳定性/配额（未做压测；单次探测全通不代表路由长期在）

**不可宣称**
- 不能宣称"重绘链路可用"——通的只是文生图，**源图保外观重绘在本机所有可用通道上都不成立**
- 不能宣称泰语本地叠加"渲染正确"——只能说测试串目测可接受，无排版引擎下不承诺任意泰语句子
- 不能宣称任何成本数字（¥/张）——网关无价格信息，DashScope 无 key
- 不能宣称 DashScope/即梦"不可用"——只验证了"无凭据不可用"，服务本身可达
- 不能宣称 magpie 网关图像路由是正式商用能力——它是本机客户端配置探测到的路由，随时可能变更
