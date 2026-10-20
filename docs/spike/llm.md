# Spike 报告：LLM 多语言 listing 生成（task-2 / M1-A）

> 运行日期：2026-10-10 18:19–18:40（Asia/Shanghai，= 10:19–10:40 UTC，周五）
> 运行机器：Windows 本机 · Python 3.12.2 · 预装 openai 2.33.0 / pydantic 2.12.5（未 pip install）
> 代码：`spike/llm/generate_listing.py`、`spike/llm/compare_models.py`
> 原始产物（全部真实调用留痕）：`spike/llm/runs/*.json`、`spike/llm/runs/console_*.txt`
> 对应设计条款：DESIGN §8 技术选型、§9 M1-A 验证项 2、§7 硬规则 4（合规词）、§3.3 生成类失败策略

---

## 0. 结论速览

| 问题 | 结论 | 证据 |
|------|------|------|
| 真实 LLM 能不能用？ | **能**。本机 magpie 网关 `POST /v1/chat/completions` 免鉴权可用；公网备路 agnes 也可用 | §2、§10 |
| schema 一次通过率 | 文案任务 **15 次真实运行里 13 次首次通过**；其中配置正确的 deepseek-flash **9/9 首过**；唯一两个非首过 = `max_tokens=4000` 配置错误的 run0、以及 pro 的 504 | §3、§4 |
| 单商品三语言耗时 | deepseek-flash：连续 3 次 22.6 / 30.8 / 36.2 s，全部真实运行合起来 **16.2–45.0 s**；glm-5-3-flash：10.6–19.8 s | §3、§8 |
| 单商品三语言成本 | deepseek-flash：约 **$0.003–0.006/商品**（按 DeepSeek 官方公开价折算，非网关实扣）；GLM/agnes **拿不到价目，无法折算** | §9 |
| 模型能否标出违规词？ | **能**。埋的 3 类陷阱（医疗/绝对化/伪造背书）每语种全部命中；**去掉提示标签后仍然全部命中**，非cue-following | §7 |
| 泰语/越南语可信度 | **未经母语者核验，M1 需人工抽检**。且已发现 GLM 泰语拼写不一致的硬伤 | §6 |
| 「文案用大杯」是否成立？ | **本次数据不支持盲目上大杯**：pro 出现 504 且小任务因预算不足失败；轻量模型质量已够用 | §8、§11 |

---

## 1. 配置：语种与端点全部不硬编码

| 项 | 取值方式 | 缺省 |
|----|----------|------|
| 语种 | `--langs` > 环境变量 `LISTING_LANGS` | `EN,TH,VI`（写在 `DEFAULT_LANGS` 常量，业务逻辑不锁死） |
| 端点 | `SHOPKEEPER_LLM_BASE_URL` / `--base-url` | `http://127.0.0.1:3425/v1` |
| 模型 | `SHOPKEEPER_LLM_MODEL` / `--model` | `group/auto-deepseek-v4-flash` |
| 密钥 | `SHOPKEEPER_LLM_API_KEY` | 占位串 `spike-no-auth-required`（实测网关不校验；**代码内无任何明文密钥**） |
| 价目 | `SHOPKEEPER_LLM_PRICE_JSON`（每 1M token 单价） | 不设 → 明确输出"无法折算" |
| 重试上限 | `--retries`，缺省 2（= 总 3 次尝试） | 对齐 §3.3 |

**语种配置化的实证**：设 `LISTING_LANGS=EN,TH,VI,ID,MS` 后一次通过，产物记录
`"langs_source": "LISTING_LANGS 环境变量"`，输出 `versions` 恰为 `['EN','TH','VI','ID','MS']`，
印尼语/马来语标题如 `Lengan Pelindung Matahari Ice Silk 46cm Unisex Elastis Anti Slip...` /
`Sarung Lengan Anti Matahari Ice Silk 46cm Unisex Anjal Anti Gelincir...`（同样未经母语核验）。
见 `runs/listing_20261010T183909_group_auto-deepseek-v4-flash_langs5.json`。

**密钥泄漏自查**：对 `spike/llm/` 全部 26 个文件按 `sk-[A-Za-z0-9]{16,}`、`LTAI...`、
`MhhxqRml`、`api_key\s*=\s*"sk` 四个模式扫描 → **0 命中**。agnes key 仅在本会话 PowerShell
里从会话环境读取，未落任何项目文件。

**Fixture**（写死，保证可复现）：1688「冰丝防晒袖套」，¥4.80，锦纶 82%+氨纶 18%，长 46 cm，
腕围 16–22 cm，38 g，4 色，OPP 袋，目标站点 = TikTok Shop 东南亚（站点未定）。
其中**故意埋 3 组合规陷阱**（原文见 `generate_listing.py` 的 `FIXTURE_PRODUCT`）。

---

## 2. 网关可用性实测

- `GET /v1/models` 免鉴权 200，返回 **37** 个模型（任务书里说的 65 个与本次实测不符，如实记录：本时刻为 37）。
- `POST /v1/chat/completions` 免鉴权可用，返回标准 `usage`（prompt/completion/total）。
- 响应 `model` 字段被网关**归一化成上游真实型号**：请求 `group/auto-deepseek-v4-flash` → 回
  `deepseek-flash`；`group/auto-deepseek-v4-pro` → `deepseek-v4-pro`；`group/auto-glm-5-3-flash` → `glm-5p3-flash`。
  这个字段是我做成本折算口径的唯一依据（§9）。
- 支持 `response_format={"type":"json_object"}`（实测 200 正常返回 JSON）；`reasoning_effort` 可透传。
- 响应带 `reasoning_content`（思考流），**不计入 `content`**，会吃 `max_tokens` 预算（§4）。
- **无任何费用接口**：`/pricing`、`/v1/pricing`、`/v1/models/pricing`、`/v1/usage` 全 404；`/v1/models` 元数据无价目字段。
  备路 `apihub.agnes-ai.cn/v1/models` 元数据同样无价目字段（10 个模型，含 `agnes-2.5-flash`）。

---

## 3. 同一任务连续 3 次运行 —— 原始数据

任务与配置（三次完全相同）：fixture 商品 / `LISTING_LANGS` 未设 → 缺省 `EN,TH,VI` /
`group/auto-deepseek-v4-flash` / `max_tokens=12000` / `reasoning_effort=low` / `temperature=0.3` / `timeout=180s` / 重试上限 2。

| # | 产物文件 | 墙钟(ms) | prompt | completion | total | schema 首次通过 | 重试次数 | 模型自查 flags(EN/TH/VI) | 词表命中 | 折算成本(USD) |
|---|----------|---------|--------|-----------|-------|----------------|---------|------------------------|---------|--------------|
| 1 | `listing_20261010T183116_..._final1.json` | 22 641 | 935 | 4 913 | 5 848 | ✅ | 0 | 6/6/6 | 0 | 0.003088 |
| 2 | `listing_20261010T183148_..._final2.json` | 30 799 | 935 | 7 507 | 8 442 | ✅ | 0 | 8/8/8 | 0 | 0.004644 |
| 3 | `listing_20261010T183226_..._final3.json` | 36 206 | 935 | 9 422 | 10 357 | ✅ | 0 | 8/8/8 | 0 | 0.005793 |
| — | 合计/区间 | 22.6–36.2 s | 935 | 4 913–9 422 | 5 848–10 357 | **3/3** | **0** | 4–8 条/语种 | **0** | 0.0031–0.0058 |

**schema 一次通过率 = 100%（3/3）**；三次全部 `finish_reason=stop`（无截断）。

## 4. 调参前的真实失败（同一任务，max_tokens=4000）—— 完整保留原始记录

产物 `runs/listing_20261010T182043_flash_run0_MAXTOKENS4000_FAILED.json`：

| attempt | 墙钟(ms) | prompt | completion | content 长度 | reasoning 长度 | finish_reason | schema | 原始 error |
|---|---|---|---|---|---|---|---|---|
| 1 | 22 643 | 935 | 4000 | **0** | 13 005 字符 | `length` | ❌ | `no JSON object found in model output` |
| 2 | 16 667 | 935 | 4000 | 5 342 | 6 521 | `length` | ❌ | `no JSON object found in model output`（JSON 被截断，无闭合括号） |
| 3 | 18 350 | 935 | 4000 | **0** | 12 705 | `length` | ❌ | 同上 |
| 合计 | 57 660 | 2 805 | 12 000 | — | — | — | **0/3** | 重试 2 次耗尽 → `status=failed`、`degraded_action=人工待办（manual_todo）` |

这是本次最重要的工程发现：**推理型模型的 `reasoning_content` 与 `content` 共享 `max_tokens` 预算**。
默认档下思考就吃掉全部 4000 token，`content` 直接为空。修法二选一（我用了两个都上）：
`max_tokens` 提到 12 000，并传 `reasoning_effort=low`。
另一次同类失败也留在产物里：pro 模型做"分类小任务"时 `max_tokens=900` 被思考吃满 → `content_len=0`、
`finish_reason=length` → schema 不合格（§8）。

**§3.3 失败策略对齐验证**：超限不抛裸异常，返回结构化对象
`{"status":"failed","degraded_action":"人工待办（manual_todo）","failure_reason":"...","attempts":[逐次原始记录]}` ——
上面 run0 就是这个形态，退出码 1，可被编排器直接落成人工待办。

---

## 5. 实际产出全文（deepseek-flash · final2 · 三语言完整）

`runs/listing_20261010T183148_group_auto-deepseek-v4-flash_final2.json`，墙钟 30.8 s，8 442 tokens。

```json
{
  "langs": ["EN", "TH", "VI"],
  "versions": [
    {
      "lang": "EN",
      "title": "Cool-Touch Nylon Spandex Arm Sleeves | Unisex Summer Cycling Fishing Driving Sun Cover, Anti-Slip Stretch Arm Guards",
      "bullets": [
        "Made of 82% nylon + 18% spandex, with a lightweight cool-touch feel for summer outdoor wear.",
        "46 cm arm coverage with a 16–22 cm stretchy wrist fit; unisex design for cycling, fishing, driving, and delivery.",
        "Silicone anti-slip strip helps keep the sleeve in place during movement; stretch knit follows the arm shape.",
        "Breathable, thin, and machine washable for repeated daily use.",
        "Available colors: light gray, pink, black, mint green; individually packed in an OPP bag."
      ],
      "description": "These unisex arm sleeves are made from 82% nylon and 18% spandex, with a cool-touch, breathable knit. The 46 cm length and 16–22 cm stretch wrist range suit summer cycling, fishing, driving, and delivery. A silicone anti-slip strip helps reduce slipping, and the fabric is machine washable for repeat use. Available in light gray, pink, black, and mint green. Individually packed in an OPP bag.",
      "compliance_flags": [
        {"term":"7天美白祛斑","category":"medical_claim","severity":"high"},
        {"term":"可治疗湿疹、皮炎","category":"medical_claim","severity":"blocker"},
        {"term":"医用级别防晒","category":"medical_claim","severity":"high"},
        {"term":"100%有效防紫外线","category":"absolute_or_exaggerated","severity":"high"},
        {"term":"全网最低价","category":"price_or_promo_risk","severity":"high"},
        {"term":"买到就是赚到","category":"absolute_or_exaggerated","severity":"medium"},
        {"term":"三甲医院皮肤科联合研发","category":"fake_endorsement","severity":"blocker"},
        {"term":"国家级权威认证","category":"fake_endorsement","severity":"blocker"}
      ]
    },
    {
      "lang": "TH",
      "title": "ปลอกแขนกันแดดผ้ายืดหยุ่น ยูนิเซ็กซ์ สำหรับปั่นจักรยาน ตกปลา ขับรถ มีแถบกันลื่น",
      "bullets": [
        "ผลิตจากไนลอน 82% และสแปนเด็กซ์ 18% ให้สัมผัสบางเบาและระบายอากาศสำหรับใช้งานกลางแจ้งในฤดูร้อน",
        "ความยาว 46 ซม. พร้อมช่วงข้อมือยืดหยุ่น 16–22 ซม. ดีไซน์ยูนิเซ็กซ์ ใช้ได้ทั้งปั่นจักรยาน ตกปลา ขับรถ และส่งอาหาร",
        "มีแถบซิลิโคนกันลื่นช่วยลดการเลื่อนหลุดขณะเคลื่อนไหว ผ้าถักโอบรับรูปแขน",
        "ผ้าบางเบา ระบายอากาศ ซักเครื่องได้ ใช้ซ้ำได้ในชีวิตประจำวัน",
        "มีสีเทาอ่อน ชมพู ดำ มินต์กรีน บรรจุแยกในถุง OPP"
      ],
      "description": "ปลอกแขนยูนิเซ็กซ์นี้ทำจากไนลอน 82% และสแปนเด็กซ์ 18% ผิวนุ่มเย็นและระบายอากาศ ความยาว 46 ซม. ช่วงข้อมือยืดหยุ่น 16–22 ซม. เหมาะกับปั่นจักรยาน ตกปลา ขับรถ และส่งอาหาร มีแถบซิลิโคนกันลื่นช่วยให้อยู่กับที่ ซักเครื่องได้และใช้ซ้ำได้ มีสีเทาอ่อน ชมพู ดำ และมินต์กรีน บรรจุแยกในถุง OPP"
    },
    {
      "lang": "VI",
      "title": "Ống tay chống nắng vải co giãn unisex mùa hè đạp xe câu cá lái xe chống trượt",
      "bullets": [
        "Chất liệu 82% nylon và 18% spandex, bề mặt mát nhẹ, thoáng khí cho hoạt động ngoài trời mùa hè.",
        "Chiều dài 46 cm, vòng cổ tay co giãn 16–22 cm, thiết kế unisex phù hợp đạp xe, câu cá, lái xe và giao hàng.",
        "Dải silicon chống trượt giúp hạn chế tuột khi vận động; vải dệt co giãn ôm theo cánh tay.",
        "Mỏng nhẹ, thoáng khí, giặt máy được và dùng lặp lại hằng ngày.",
        "Màu sắc: xám nhạt, hồng, đen, xanh bạc hà; đóng gói riêng trong túi OPP."
      ],
      "description": "Ống tay unisex này làm từ 82% nylon và 18% spandex, bề mặt mát nhẹ và thoáng khí. Chiều dài 46 cm, vòng cổ tay co giãn 16–22 cm, phù hợp đạp xe, câu cá, lái xe và giao hàng. Dải silicon chống trượt giúp hạn chế tuột, có thể giặt máy và dùng lặp lại. Các màu: xám nhạt, hồng, đen, xanh bạc hà. Đóng gói riêng trong túi OPP."
    }
  ]
}
```

（**TH/VI 的 `compliance_flags` 与 EN 逐条完全相同**——11 次成功运行全部如此。
flag 的 `term` 一律是**中文原文**（因为陷阱埋在中文卖点里，`location=source_input`），
`reason` 字段才用各语种本地语言书写。含义：模型把"源数据违规项"当作与输出语种无关的同一份清单来报，
**不是**逐语种独立复检其自身输出。落库时按 1 份源数据违规清单 + N 份译文复用即可，别按语种重复计费。
完整原文见产物文件。）

**幻觉专项检查（扩大样本后重扫）**：对 **11 次成功产出文案的运行、共 35 个语种版本**
（EN/TH/VI 为主，含 ID/MS）正则扫描 fixture 里不存在的事实
（UPF/SPF 数值、50+、检测报告、专利、销量/已售、评分、产地、终身/保证、免运费、
以及擅自新增的 `clinic / dermatolog / hospital / bệnh viện / โรงพยาบาล` 类背书词）→ **35/35 无命中**。
文案里出现的数字全部可回溯到 fixture（82%/18%、46 cm、16–22 cm、38 g、4 色、OPP）。
注意：源数据自述"冰丝"，模型写成 "Ice Silk / ผ้าไหมเย็น / vải ice silk"——**这是搬运供应商话术**，
不是模型幻觉，但属于 M1 要人工核对的"源数据合规"问题。

---

## 6. 泰语 / 越南语可信度评估（诚实版）

**结论：未经母语者核验，M1 需人工抽检。本报告不宣称任何"优秀/地道"评级。**

本机没有泰语/越南语母语者，也没有可信的自动语言质量评测，因此只能给以下**非母语**层面的观察：

1. **形态与文字系统正确**：泰语无空格分词、声调符号与 vowel 位置看起来成文（如
   `ปลอกแขนกันแดดผ้ายืดหยุ่น`），越南语声调符（`ỐNG tay... co giãn`, `hằng ngày`）成文，
   没有"乱码/拉丁转写冒充"这类低级失败。
2. **已发现的具体硬伤（GLM-5-3-flash）**：同一条泰语 bullet 写 `น้ำหนักเบวเพียง 38 กรัม`
   （`เบว` 疑为 `เบา` 拼错），而同一版 description 又写对 `น้ำหนักเบาเพียง 38 กรัมต่อคู่`
   ——**同一文档内前后拼写不一致**，属可由母语者一眼看出的错误。见
   `runs/listing_20261010T183851_group_auto-glm-5-3-flash_glmfull.json`。
3. **借词策略不确定**：VI 文本大量保留 `unisex / nylon / spandex / silicon / OPP`，TH 文本用
   `ยูนิเซ็กซ์ / ไนลอน / สแปนเด็กซ์ / ซิลิโคน`。哪个更符合目标站买家搜索习惯，**本机无法判断**，
   需要 M1 用泰国/越南本地热销 listing 做对照 + 母语者抽检。
4. **营销语感无法评估**：标题是否符合 TikTok Shop TH/VN 的关键词前置习惯、是否有语域冒犯，
   本机无参照物，**不评估、不打分**。
5. **抽检口径建议**（对齐 M1 Done 条件"人工抽检通过率 ≥ 80%"）：
   每语种 20 条起步，母语者按「拼写/语法 0 硬伤 + 关键词可读 + 无违规表述」三问打通过/不通过，
   不通过率 > 20% 则该语种强制走"人工改写"而非"人工放行"。

（ID/MS 同样未经核验，且站点未定 —— 见 §12 未决问题 1。）

---

## 7. 合规词过滤：模型自查 vs 词表强制过滤（§7.4 双轨）

### 7.1 埋的测试词（fixture 内 3 组，含医疗/疗效声明类）

| 编号 | 原文（我埋进卖点的） | 风险类目 | 期望动作 |
|------|----------------------|----------|----------|
| T1 | `7天美白祛斑，可治疗湿疹、皮炎，医用级别防晒` | 医疗/疗效声明 | 剔除 + 标 flag |
| T2 | `100%有效防紫外线，全网最低价，买到就是赚到` | 绝对化/夸大 + 价格话术 | 剔除 + 标 flag |
| T3 | `三甲医院皮肤科联合研发，国家级权威认证` | 伪造权威背书 | 剔除 + 标 flag |

### 7.2 模型自查：**能命中**，且不是靠提示标签

- 3 次连续运行 + 5 语种运行 + 备路运行：**每组 3 个语种版本都标出了 T1/T2/T3 三类**，
  `location=source_input`，`severity` 给到 `blocker`（治疗湿疹、三甲医院联合研发）。
- 每次运行**成品文案里 0 条违规表述残留**（词表重扫 0 命中，见 7.3）。
- **去提示对照实验**（关键）：fixture 里原本带 `T1-埋点：` 前缀，可能构成 cue。我用脚本把前缀
  剥掉、卖点变成纯自然中文后重跑：

  | 模型 | 墙钟 | total tokens | EN/TH/VI flags | 三类陷阱命中 |
  |---|---|---|---|---|
  | deepseek-flash | 17.6 s | 5 429 | 8/8/8 | 医疗 ✅ 绝对化 ✅ 背书 ✅（且把 T1 拆成 `7天美白祛斑`/`可治疗湿疹、皮炎`/`医用级别防晒` 三条） |
  | glm-5-3-flash | 19.8 s | 3 096 | 4/4/4 | 三类全 ✅（按整句给，粒度较粗） |

  产物：`listing_unlabeled_group_auto-deepseek-v4-flash.json`、`listing_unlabeled_group_auto-glm-5-3-flash.json`。
  → **判定：命中是模型真识别，不是标签暗示。**
- **但粒度不稳定**：同一模型同一 prompt，flag 条数在 3–8 条之间浮动（有的合并同类、有的逐条拆），
  `severity` 也会飘（`全网最低价` 在 final1 是 medium、final2 是 high）。
  → **不能把"模型自查"当唯一防线**：条数少 ≠ 风险低，编排器不能按 flag 数量做放行判定。

### 7.3 词表强制过滤（第二轨）

`COMPLIANCE_VOCAB` 4 类最小词表（中/英/泰/越混排）+ `vocab_filter()`：
ASCII 词条**词边界正则**匹配，中文/泰文/越南文按子串匹配。命中即拦，落到 listing 上架前的双重校验。

离线自检实测（`--selftest`，构造样例，非模型输出）：

- **查全**：构造违规文案（3 语种，明确标注为构造样例）→ **13 条命中**，类目 `medical_claim`/`absolute_or_exaggerated`，
  EN 5 / TH 4 / VI 4。逐条原文（`runs/selftest_20261010T183038.json`）：

  | 语种 | 命中词 | 类目 | 上下文片段 |
  |---|---|---|---|
  | EN | `eczema` / `dermatitis` | medical_claim | `...cures eczema and dermatitis, medica...` |
  | EN | `best` / `100%` / `guaranteed` | absolute_or_exaggerated | `Ice Silk Arm Sleeves. Best UV protection, 100% guaranteed...` |
  | TH | `โรค` / `湿疹` | medical_claim | `ปลอกแขนผ้าเย็น รักษาโรค湿疹 ดีที่สุด 100%` |
  | TH | `ดีที่สุด` / `100%` | absolute_or_exaggerated | 同上 |
  | VI | `điều trị` | medical_claim | `Ống tay lụa băng, điều trị viêm da, 100% hiệu quả, tốt nhất.` |
  | VI | `tốt nhất` / `100%` / `100% hiệu quả` | absolute_or_exaggerated | 同上 |

- **查准**：合法文案 `"The biggest value in our session: secure knit arm covers..."` →
  词边界匹配 **0 命中**；而同样的词表用**朴素子串匹配会产生 1 个误杀**：`cure` 命中 `se|cure|`。
  （我最初注释里写的 `best`→`biggest` 是**错的**，`biggest` 不含 `best`；已由 selftest 更正为可复现的 `cure`→`secure`。）
- **已知漏检（词形，逐条实测）**：单独构造 `"This cures claim"` / `"This medically claim"` → **0 命中**；
  `"This medical claim"` → ✅ 命中 `medical`。
  即严格词边界换来的代价是**英文词形变体全漏**（`cures/medically/protection` 均不命中，本次构造文案里的
  `cures eczema` 是靠 `eczema` 捞回来的，`medically proven` 是靠词表里另一条 `clinically proven`… 也**没捞回来**，
  该条在本次 13 命中里不存在）。M1-B 词表必须**带词形变体或做词干化/分词**，否则英文轨单独使用会漏。
- **本次 11 次成功产出的成品文案（35 个语种版本），用最终词表（词边界版）重算 → 全部 0 命中**
  （`--recheck-runs` 结果，见各产物 `vocab_filter_recomputed_with` 字段；`compare_models.py` 里 3 次运行的
  `vocab_filter_hits` 同样为 0）。
  注意：0 命中的含义是"没出现词表里的词"，**不等于**"绝对合规"——上面的词形漏检就是反例。

### 7.4 双轨结论（§7.4 必须双轨的实测理由）

两条轨各自会漏不同的东西，**必须同时存在**：

| 场景 | 模型自查 | 词表过滤 |
|------|----------|----------|
| 源数据里的违规卖点（T1/T2/T3） | ✅ 命中并主动改写 | ⚠️ 只能扫成品，源数据要另扫 |
| 违规词以**没在词表里的新说法**出现 | ✅ 语义泛化能力强 | ❌ 漏（如 `cures/medically` 本次即漏） |
| 模型自查粒度飘 / 输出被截断 / 重试耗尽 | ❌ 可能 0 flag 或无产出 | ✅ 确定性、与模型无关、可审计 |
| 需要留痕给闸门/审计 | 需要 schema 化（已做 `ComplianceFlag`） | 天然结构化，可回写 `gate_decisions` |

→ **实现口径**：模型 flag 只作"改写字段 + 人工提示"，**放行判定以词表 0 命中为准**；
两者都过才允许进上架审批。M1-B 起 `listing_versions` 要同时存 `model_flags` 与 `vocab_hits` 两列，
不要只存一个"合规通过"布尔。

**另需 M1-B 补的口子（本次实测发现的结构性盲区）**：模型报的违规清单是**源数据清单**
（11 次运行里 `location` 全是 `source_input`，且 EN/TH/VI 三份 flag 逐条相同），
即模型**不会独立复检自己每语种译文里新冒出来的违规表述**（例如译文里真出现 `รักษาโรค`/`điều trị`
这类泰/越医疗声明，本次样本没有，但无机制保证）。
→ 译文侧的违规只能靠**词表强制过滤**兜（本次 35 个语种版本扫到 0 命中），
   或加一次"逐语种译文复检"调用。这是"双轨必须同时在"最硬的结构性理由，不是可选项。

---

## 8. 模型横向对比（`compare_models.py`，同一 fixture，同一 prompt）

产物：`runs/compare_20261010T183800.json`

### A) listing 文案生成（EN/TH/VI）

| 模型（请求名 → 网关返回名） | status | 尝试数 | 首次 schema | 墙钟 | prompt | compl | total | flags EN/TH/VI | 备注 |
|---|---|---|---|---|---|---|---|---|---|
| `group/auto-deepseek-v4-pro` → `deepseek-v4-pro` | ok | **2** | ❌ | **240.6 s** | 988 | 2 996 | 3 984 | 3/3/3 | **attempt1 = `504 Gateway Timeout`（上游 giikin），184.8 s 才超时返回**；attempt2 55.7 s 成功 |
| `group/auto-deepseek-v4-flash` → `deepseek-flash` | ok | 1 | ✅ | 45.0 s | 935 | 11 540 | 12 475 | 8/8/8 | 思考 29 401 字符 |
| `group/auto-glm-5-3-flash` → `glm-5p3-flash` | ok | 1 | ✅ | **10.6 s** | 899 | 1 766 | **2 665** | 3/3/3 | 思考仅 71 字符；**泰语有拼写硬伤（§6.2）** |

pro 的 504 原文（结构化记录在 `attempts[0].error`）：
```
InternalServerError: Error code: 504 - {'error': {'code': None,
'message': 'giikin: 504 Gateway Timeout', 'param': None, 'type': 'api_error'}}
```

### B) 分类 / 评分小任务（选品官风格，严格 JSON，max_tokens=900，temperature=0）

| 模型 | 墙钟 | prompt | compl | total | schema | 陷阱类目命中 | risk/competition/gap | decision |
|---|---|---|---|---|---|---|---|---|
| `group/auto-deepseek-v4-pro` | 20.3 s | 615 | 900 | 1 515 | **❌** | 无（无产出可判） | — | — （`content_len=0`，`finish_reason=length`：**思考吃满 900 预算**） |
| `group/auto-deepseek-v4-flash` | 3.8 s | 562 | 734 | 1 296 | ✅ | 医疗+绝对化+背书 全中 | 95/78/82 | `reject`（`reason` 写得最完整：逐条点名三类违规并推出"下架、扣分、冻结店铺"后果） |
| `group/auto-glm-5-3-flash` | **2.7 s** | 546 | 163 | **709** | ✅ | 医疗+绝对化+背书 全中 | 95/70/75 | `reject`（`reason` 明显更短，结论一致） |

两模型的 `banned_claims_found` 在此任务上**完全相同**，都是按整句给 3 条（不是逐词拆分）：
`{"category_l1":"服饰配件","category_l2":"防晒袖套/冰袖","price_gap_score":82,"competition_score":78,`
`"compliance_risk":95,"decision":"reject",`
`"banned_claims_found":["7天美白祛斑，可治疗湿疹、皮炎，医用级别防晒",`
`"100%有效防紫外线，全网最低价，买到就是赚到","三甲医院皮肤科联合研发，国家级权威认证"]}`

（注意与 §7.2 的差异：**listing 任务**里 flash 会把违规拆到 8 条、GLM 只给 3 条整句；
**classify 任务**里两者都给 3 条整句。即"粒度"随任务形态变，进一步证明不能拿 flag 条数当判据。）

### C) 公网备路（`https://apihub.agnes-ai.cn/v1`，key 走环境变量）

| 模型 | 墙钟 | prompt | compl | total | 首次 schema | flags | 词表命中 |
|---|---|---|---|---|---|---|---|
| `agnes-2.5-flash` | 28.1 s | 1 191 | 2 911 | 4 102 | ✅ | 3/3/3 | 0 |

→ **备路可用**，可作为 magpie 不可达时的降级出口（同一套代码，只换 3 个环境变量）。

---

## 9. 成本：能折算的与不能折算的

**网关不收费信息**：`/v1/chat/completions` 无 cost 字段，`/pricing` 等 4 个路径 404，
`/v1/models` 与 agnes `/v1/models` 元数据均无价目字段。harness 自带 `web_search` 工具本次**不可用**
（其自身 DeepSeek key 401：`Authentication Fails, Your api key: ****LyOj is invalid`），
智谱定价页 `open.bigmodel.cn/pricing` 为 JS 渲染，`web_fetch` 取不到表格数据。

**能折算的**：DeepSeek 官方文档 `api-docs.deepseek.com/quick_start/pricing` 给出
`deepseek-flash` 与 `deepseek-v4-pro` 的 USD/1M tokens 价目（本次 10:3x UTC 周五 = **off-peak**）：

| 上游模型 | 输入(cache miss) off-peak | 输出 off-peak |
|---|---|---|
| deepseek-flash | $0.15 | $0.60 |
| deepseek-v4-pro | $0.66 | $1.98 |

**重要口径声明（别把这张表当账）**：
1. 这是**厂商公开牌价折算**，**不是 magpie 网关的实际扣费**（内部分发网关价格不可知，可能为 0 或另计）；
2. 按 cache-miss 计是**上界**——实测连续 3 次运行的 `prompt_tokens_details.cached_tokens` 均为 **768**
   （935 prompt 里 768 命中缓存，官方 cache-hit 输入价 $0.003/M），按 cache-hit 重算 final2 为
   $0.00453 vs 表中 $0.004644，**差异 <3%**，不改变任何结论；
3. **`reasoning_tokens` 在 DeepSeek 路由上全程报 0**（final1–3 的 `reasoning_content` 实测长
   7 293 / 16 717 / 21 787 **字符**，而 `completion_tokens_details.reasoning_tokens` 仍是 0；
   同一次 `LISTING_LANGS=EN,TH,VI,ID,MS` 运行 `reasoning_content` 长度为 **0**，即思考量在 0–21 k 字符间大幅浮动），
   只有 GLM 报了 12/62/170、agnes 报了 1000 —— **网关 token 归因不可信，别用它做成本核算或预算告警**，
   只能用 `completion_tokens` 总量（思考已含在里面，所以折算金额本身还成立）。
4. **GLM 与 agnes 拿不到价目，无法折算**，本报告只给它们的 token 数，不给金额；
   `runs/cost_estimate.json` 里对应行的 `cost_available=false` 与原因字符串。

**单商品三语言（EN/TH/VI）结论**（deepseek-flash，官方牌价口径）：

| 方案 | tokens/商品 | 折算 | 耗时/商品 |
|---|---|---|---|
| 主路 flash 文案 | 5 848–10 357 | **$0.0031–0.0058**（约 ¥0.02–0.04） | 22.6–45.0 s（含对比那轮 45 s） |
| flash 分类/评分 | 709–1 296 | $0.0005/次 | 3.8 s |
| pro 文案 | 3 984 | $0.0066 | 55.7–240.6 s（含 504 重试） |
| glm-5-3-flash 文案 | 2 665–3 096 | **无法折算（无价目）** | 10.6–19.8 s |

对照 §10「M1 费用预估 < ¥100」：本报告金额是 **USD**，折人民币需按 **1 USD ≈ 7.1 CNY 的假设汇率**
（本次**未调用汇率 API 核实**，属手工假设，M1 财务官 Ledger 接真实汇率后要重算）。
按该假设，flash 文案 ¥0.02–0.04/商品 + 分类 ¥0.004/商品 → **1 000 个商品约 ¥24–44**，在预算内；
但**若按 pro 跑全量文案，输入价是 flash 的 4.4 倍（$0.66 vs $0.15）且延迟不可控**，预算与时效都撑不住。

---

## 10. Schema 强制校验与失败降级 —— 离线自检（9 例全过）

`python spike/llm/generate_listing.py --selftest`（**明确标注：不打网关、构造样例，不作为真实调用证据**），
产物 `runs/selftest_20261010T183038.json`：

| # | 用例 | 期望 | 实测 |
|---|---|---|---|
| 1 | 非 JSON 文本 | 拒绝 | ✅ `no JSON object found` |
| 2 | 缺最后一个语种版本 | 拒绝 | ✅ |
| 3 | bullets 只有 2 条 | 拒绝 | ✅ `List should have at least 3 items` |
| 4 | title 超 400 字符 | 拒绝 | ✅ `String should have at most 400 characters` |
| 5 | `compliance_flags` 是字符串数组 | 通过并归一化成对象 | ✅ `flag_coerced=true` |
| 6 | 持续非法格式 | attempts=3, status=failed | ✅ 重试恰好 2 次，返回 `degraded_action=人工待办` |
| 7 | 首次失败第 2 次成功 | attempts=2, status=ok | ✅ |
| 8 | 词表查全（构造违规文案） | 多类目命中 | ✅ 13 命中 / 2 类目 |
| 9 | 词表查准（合法文案+干扰） | 词边界 0 命中 | ✅ 0 命中（朴素子串=1 误杀） |

自检的价值是**当场抓出 2 个真 bug**（都是我在 §4 三次成功运行之后、最终代码之前修的，修完才重跑 3 次）：

1. **`compliance_flags` 字符串数组会误判为不合格式**：模型很常返回 `["治疗湿疹"]` 这种裸字符串数组，
   原 schema 直接 pydantic `model_type` 报错 → 白白烧掉 2 次重试。
   修法：`Annotated[ComplianceFlag, BeforeValidator(_coerce_flag)]`（用例 5）。
2. **词表子串匹配误杀**：`cure` 命中 `secure`。修法：ASCII 词条词边界正则（用例 9）。

schema 字段口径：`title` 8–400 字符、`bullets` 3–8 条且逐条非空、`description` 40–4 000 字符、
`lang` 必须等于配置集合且无缺失/重复、`compliance_flags[].severity` 归一到 `low|medium|high|blocker`。

---

## 11. 选型结论（对应 DESIGN §8「文案用大杯，分类用小杯」）

| 用途 | 推荐 | 理由（全部来自本报告数据） | 兜底 |
|------|------|---------------------------|------|
| **listing 文案（EN/TH/VI 等多语）** | **`group/auto-deepseek-v4-flash`（主）**，`group/auto-glm-5-3-flash` 作**成本敏感的批量备选** | flash：配置正确的 **9 次文案运行全部 schema 首过（9/9）**、违规词三类全中、35 个语种版本 0 处 fixture 外事实、$0.003–0.006/商品、16–45 s。GLM：更快更省（10.6 s / 2 665 token、3/3 首过）但**泰语拼写有硬伤**且 flags 粒度粗（3 条整句） | `agnes-2.5-flash`（公网备路，实测可用，28.1 s，首过） |
| **分类 / 评分 / 合规初筛（Scout、路由判定）** | **`group/auto-glm-5-3-flash`（小杯）** | 2.7 s / 709 token / schema ✅ / 陷阱三类全命中，比 flash 便宜 45%、快 30%，短结构化输出任务上质量差距看不出 | flash（2.7→3.8 s，reason 更详尽） |
| **大杯 `auto-deepseek-v4-pro`** | **本次不建议进主链路** | 与 §8 预设相反，但这是数据：① 上游 504 一次（184.8 s 才失败）；② 总墙钟 240.6 s 是 flash 的 5 倍；③ **连 900-token 的小任务都因思考吃满预算而 0 产出**。真要上大杯，必须给它单独配 `max_tokens ≥ 8 000` + 更短 prompt + 更长 timeout，并把 504 计入熔断预算 | 仅用于"人工抽检判为不合格、要求重写的高价值 SKU"，且异步跑 |
| **工程配置底线（所有模型通用）** | `max_tokens ≥ 8 000`（文案 ≥ 12 000）、`reasoning_effort=low`、`timeout ≥ 240 s`、重试 2 次→人工待办 | §4 的 4000 预算 0/3 与 pro 小任务 0 产出，都是同一根因 | — |

**给 M1-B 的三条直接动作**：
1. `LlmAdapter` 的 `max_tokens` / `reasoning_effort` **必须是按任务的路由配置项**，不能一个全局常量——
   否则 §4 的 0/3 会以"偶发失败"的形式进生产，且每次失败白烧 12 000 token。
2. 成本/预算告警**不要用 `reasoning_tokens`**（§9.3 该字段在本网关不可信），用 `completion_tokens` 总量。
3. 合规判定实现成 §7.4 的双轨串联：**词表 0 命中是硬闸**，模型 flag 只作改写说明与人工提示，
   两者分别落库，不合并成一个布尔。

---

## 12. 三栏收尾

### ✅ 已验证（有当次运行输出支撑）
- magpie `POST /v1/chat/completions` 免鉴权真实可用；OpenAI 兼容 + `usage` 完整；支持 `response_format=json_object` 与 `reasoning_effort` 透传（§2）。
- 三语言（EN/TH/VI）listing 端到端产出严格 JSON，**同一配置连续 3 次运行 3/3 首次通过 schema**；扩展到 5 语种同样首过（§3、§1）。
- 语种由 `LISTING_LANGS`/`--langs` 配置驱动，业务代码未硬编码语种（§1）。
- 端点/模型/key 全环境变量；`spike/llm/` 全目录密钥扫描 0 命中（§1）。
- §3.3 生成类失败策略：重试 2 次上限、超限返回结构化 `failed` + `degraded_action=人工待办`，不抛裸异常（§4 真实触发过一次、§10 用例 6/7 覆盖）。
- 模型自查能命中医疗/绝对化/伪造背书三类埋点，**去掉 `T1-埋点` 提示后仍命中**（§7.2）。
- 成品文案在 **11 次成功运行（35 个语种版本）** 里 **0 条违规词残留**（词表重扫），且 **0 处 fixture 外事实**（UPF/认证/销量/背书类）（§5、§7）。
- 词表过滤双轨价值：子串误杀 `cure→secure` 可由词边界消除；同时暴露 `cures/medically` 词形漏检（§7.3）。
- 单商品 token/墙钟实测区间，以及 DeepSeek 官方牌价口径下的折算金额（§3、§9）。
- 公网备路 agnes-2.5-flash 同一套代码可跑通（§8C）。

### 🚫 刻意未验（超出 task-2 scope，M2+ 再开）
- 未做真实 1688 页面解析 → 商品信息是**写死的 fixture**（task-1 的 scope，`spike/alibaba_parse/`）。
- 未验图像翻译/重绘 API（task-3，`spike/image/`）。
- 未做 mock 适配器与契约测试（task-5）。
- 未验上架/改价等平台写通道、审批闸门、履约与对账（M2/M3）。
- 未做并发/压测与限流（未测网关 QPS 上限，也未测多商品批量跑的真实墙钟；单次串行样本 n=1~3）。
- 未验 TikTok Shop 各站点**官方**禁词清单与标题字数上限（词表是我手工构造的最小集，不是平台词表）。
- 未验 agnes 之外的其他公网备路、未验 magpie 故障时自动切换（无故障注入）。
- 未验 prompt 注入/越狱鲁棒性（源数据含恶意指令时模型是否被带偏）。
- 未验成本真实账单（拿不到网关扣费数据；见"不可宣称"）。

### ⛔ 不可宣称（本次证据不足以支撑的话）
- **不能宣称泰语/越南语（及印尼/马来语）文案"地道/优秀/可直接上架"** —— 无母语者核验，且已知 GLM 泰语拼写硬伤；M1 必须人工抽检。
- **不能宣称"单商品成本 = X 元"** —— 网关不回传费用，本报告金额是**厂商公开牌价折算**，非实扣；GLM/agnes 无法折算。
- **不能宣称模型自查的合规覆盖率可靠** —— flag 条数 3–8 浮动、severity 会飘，只可作提示，不可作放行判据。
- **不能宣称 schema 一次通过率 = 100% 是稳态** —— 文案任务真实样本只有 15 次运行（含 pro 的 504 与 run0 的 0/3），分类任务只有 3 次；且 run0 与 pro 小任务证明通过率对 `max_tokens` 配置**极度敏感**。
- **不能宣称 pro「质量更好所以值得」** —— pro 本次只成功 1 次（外加 1 次 504、1 次小任务 0 产出），质量未做盲评对比，且慢 5 倍。
- **不能宣称网关容量/并发/稳定性 SLA** —— 未压测；只观察到 1 次上游 504。
- **不能宣称 `/v1/models` 有 65 个模型** —— 本时刻实测 37 个（模型清单会随网关路由变化）。
- **不能宣称"该模型路由 = 某个确定上游版本"** —— `group/auto-*` 是路由组，网关可能改派（响应里 `model` 字段归一化即为证据），换供应商后需重跑本 spike。

---

## 附：产物索引（本地探针目录 `spike/llm/`，不随公开快照发布）

| 文件 | 内容 |
|------|------|
| `generate_listing.py` | 主脚本：fixture / 语种配置 / pydantic 严格 schema / 重试 2 次 / 结构化失败 / 词表双轨 / 耗时+token+成本折算 / `--selftest` / `--recheck-runs` |
| `compare_models.py` | 3 模型 × 2 任务（listing + 分类评分）对比，含 `trap_coverage()` 埋点命中判定 |
| `runs/listing_*_final{1,2,3}.json` | 连续 3 次运行原始留痕（§3） |
| `runs/listing_*_run{1,2,3}.json` | 修复 schema bug 前同配置 3 次（§4 补充） |
| `runs/listing_..._run0_MAXTOKENS4000_FAILED.json` | 调参前 0/3 失败全记录（§4） |
| `runs/compare_20261010T183800.json` | 模型对比原始数据（§8） |
| `runs/listing_unlabeled_*.json` | 去提示标签的合规埋点对照实验（§7.2） |
| `runs/listing_..._langs5.json` | `LISTING_LANGS=EN,TH,VI,ID,MS` 配置驱动证据（§1） |
| `runs/listing_..._backup.json` | agnes 公网备路（§8C） |
| `runs/selftest_20261010T183038.json` | 9 例离线自检（§10） |
| `runs/cost_estimate.json` | 逐次运行的成本折算依据与"不可折算"原因（§9） |
| `runs/console_*.txt` | 对应运行的控制台原文 |
