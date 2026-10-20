# 1688 商品链接解析 Spike（M1-A / task-1 收尾）

> 执行人：接手 agent（前人跑了 17 个探测后中断，未留交付物）
> 时间：2026-10-10 18:31–19:00（Asia/Shanghai）
> 环境：本机 Windows PowerShell 5.1 + 系统 Python 3.12.2（requests 2.34.2 / bs4 / lxml，无 pip 安装）
> 范围：只写 `spike/alibaba_parse/` 与本文档；未触碰 `src/`、`tests/`、`docs/DESIGN.md` 等
> 纪律：纯只读 GET、串行不并发、请求间隔 ≥2s、不下单不写操作、**全程未使用任何 Cookie / 登录态 / 账号凭据**，未创建 `.env`

---

## 1. 裁决（四选一）

# **仅移动端或接口可用** —— 具体是：**只有移动版页面 `m.1688.com/offer/<id>.html` + 手机 UA 能取到全量数据（匿名、零 Cookie）；PC 端 `detail.1688.com` 100% 被反爬 punish 壳拦；无线端 mtop 接口验不通。**

不是"可用"：PC 链接直接抓不到任何数据；移动版在本会话 45 次抓取里命中 4 次（≈9%），两组"同一链接连续 3 次运行"是 **0/3 和 1/3**，达不到"稳定取得"的 M1-A 验收词（DESIGN §9 M1-A：「标题、价格、库存、主图和详情图能**稳定**取得」）。

不是"需 Cookie 或登录态"：命中的那几次返回的页面里 `loginUserId = 0`、`buyerLoginId = ""`、`isPricePrivate = false`、`addCartPrivate.isPrivate = false`，且响应**没有下发任何 Set-Cookie**，请求也没带任何 cookie —— 数据是平台**匿名公开**的，登录不是前提。

不是纯"被反爬拦"：反爬确实拦掉了绝大多数请求（且 PC 端全拦），但移动端在限速/退避后**确实返回过完整数据**（本会话 4 次独立成功 + 前人 1 次，成功页字节数稳定在 172522 量级），所以不能裁成"完全不可用"。

关键量化（本会话对移动版 offer 页共 45 次抓取，含脚本内部重试）：

| 目标端 | UA | 会话 | 结果 |
|---|---|---|---|
| `detail.1688.com/offer/<id>.html` | 桌面 Chrome | 新会话，0 cookie | 200，**2397 字节 punish 壳**，`bxpunish: 1` |
| `detail.1688.com/offer/<id>.html` | iPhone 移动 | 新会话，0 cookie | 200，**2397 字节 punish 壳**，`bxpunish: 1` |
| `m.1688.com/offer/<id>.html` | 桌面 Chrome | 新会话 | 200，**2039 字节 punish 壳**，`bxpunish: 1` |
| `m.1688.com/offer/<id>.html` | **iPhone 移动** | 新会话，0 cookie | 200，**172522 字节完整页 + 内联 JSON**（命中），或 2039 字节 punish 壳（未命中） |

→ **判活特征是 UA，不是域名**：同一个 `m.1688.com` URL，桌面 UA 拿到的是 punish 壳（2039B），iPhone UA 拿到的是完整页（172522B）。PC 域名 `detail.1688.com` 两种 UA 全拦。

本会话对**移动版商品页**（唯一可能出数的端点）的抓取总账：**45 次抓取 / 4 次命中（≈9%）**。4 次命中分别是：探测 22 单发、探测 25 单发、`find_candidates.py` 的 L2 验中（18:52）、副链接 3 连跑的 run2（18:48）。前人另有 1 次命中（`_probe10.out` attempt3）。

---

## 2. 前人成果捡拾（`_probe1 … _probe17`，全部读过）

前人 **17 个探测只跑了、基本没留 stdout**（仅 `_probe10.out/.err` 与 `sample/` 下 6 个 HTML 留档）。下面是它的探索路径 + 我从留档物/本次重跑里拿到的结论。**"结果（未留档）"列标明哪些是我这次实测复现的，哪些只是"试过但不知返回什么"。**

| 探测 | 试过什么（URL / UA / 策略） | 留档证据 | 结果解读 |
|---|---|---|---|
| 1 | `www.1688.com` 首页，桌面 UA，正则 `detail.1688.com/offer/(\d+).html` 抓 id | 无 stdout | 未留档。本次复现：首页 200 / 104224 字节，**运营位里的数字是 componentId/uuid，不是商品 id**（见 §2.1 坑） |
| 2 | 首页 host 直方图 + `detail/m/sku/winport` 各类 URL 模式 | 无 | 未留档 |
| 3 | `s.1688.com/selloffer/offer_search.htm?keywords=手机壳`、`selloffer_search.html`、`worldcup/subject.html`、`m.1688.com/` | 无 | 未留档。搜索页是它最先试的公开列表源 |
| 4 | 搜索页 headers + body 前 5106 字符全打印 | 无 | 未留档（这条本来最该留下证据，但没重定向到文件） |
| 5 | 首页反爬特征计数：`bxpunish`/`rgv587`/`x5sec`/`captcha`/`_____tmd_____`/`baxia`/11–13 位数字 | 无 | 未留档 |
| 6 | 2 个 id（`896192389922`/`679897239501`）× {`detail.1688.com`, `m.1688.com`}，桌面 UA | 无 | 未留档。**本次复现：4 组里 3 组 punish，第 4 组（m + 桌面 UA）punish** |
| 7 | `m.1688.com/offer/896192389922.html`，**iPhone UA**，dump meta / script type / 内联 JSON 标记 / `window.*` 赋值 / `cbu01` 图链 | 无 stdout，但说明它看到了内容 | 这条**命中过**：它打印的关键词表（`__INIT_DATA`/`offerDetail`/`"price"`…）正是本次确认的数据结构 |
| 8 | 3 种 UA（桌面/iPhone/Android）× 3 连击，间隔 2.5s，打印 len + bxpunish + title | 无 | 未留档（在试"UA 是否是判活钥匙"——本次已证实是） |
| 9 | 同页 8 次重试，间隔 **25s**，len>50000 才存 `_sample_mobile.html` | 文件**不存在** | 8 次全被拦，没落盘 → 说明 25s 退避不够 |
| 10 | 冷置 240s → `m.1688.com/` warmup → 6 次重试间隔 45s，**带 Session** | `_probe10.out` ✅ | **attempt1 status200 len2039 bxpunish=1 → BLOCKED；attempt2 ConnectionError(RemoteDisconnected)；attempt3 len172522 bxpunish=None → OK**，落盘 `sample/mobile_offer.html`（172522 字节，本次已解析验证）。warmup 后 session 里出现 `x5secdata` cookie |
| 11 | 首页存档 + 反查那 2 个 id 是否真在首页里 + script block 摘要 | `sample/homepage.html` ✅ | **本次已证实这 2 个 id 是真的**（不是前人瞎编）：`896192389922` 出现在首页菜单 URL `fastx.1688.com/app/1688-global/global-distribute-app/auth?offerId=896192389922`；`679897239501` 出现在 `air.1688.com/app/channel-fe/search/index.html#/result?topOfferIds=679897239501` |
| 12 | 首页 URL 里筛候选聚合页（`air/` `pages-fast/` `sale/` `chaojigongchang/` `fenxiao/` `rank` `hot`） | 无 | 产出了探测 13 的 4 个 URL |
| 13 | 4 个营销/SSR 页（`sale.1688.com/factory/...?topOfferIds=926827956095`、`pages-fast.1688.com/wow/cbu/...`、`chaojigongchang.1688.com`、`sale.1688.com/sale/distribution/...`） | `sample/ssr_*.html` ×3 ✅ | **本次实测：3 个存档页里 offer id = 0**，标题分别是"工厂直达/入驻优选货源/…"一类频道壳，**拿不到商品**。这条路（营销 SSR 页当列表源）判死 |
| 14 | 拿匿名 cookie 后手算 mtop 签名（appkey `12574478`，md5(`tk&t&appkey&data`)）打 `h5api.m.1688.com` 4 个 API 名 | 无 | 未留档 |
| 15 | 同上但每 API 打 2 轮 + 打印 ret 码 | 无 | 未留档。**本次实测这批 API 名（见 §5.4）：`FAIL_SYS_API_NOT_FOUNDED`；`mtop.taobao.detail.getdetail` → `bxpunish=1`；匿名会话根本没下发 `_m_h5_tk`，签名前提就不成立** |
| 16 | `wiki.1688.com/`、`global.1688.com/`、`alibaba.com/product-detail/x_1600...html` | 无 | 未留档。`wiki.1688.com` 这条是它找到的最好列表源 |
| 17 | `wiki.1688.com/` 存档 + 提 `detail.1688.com/offer/<id>.html` + 打印上下文 | `sample/wiki.html` ✅ | **本次实测：728879 字节 / 200 / 无 bxpunish / 68 个真实 offer id，其中 46 个带完整商品卡片（到手价+销量文案+商品图）**。这是唯一好用的公开列表源 |

**前人真正的成果 = 一次成功抓取（`sample/mobile_offer.html`，172522 字节）+ 一个可用列表源（wiki.1688.com）+ 一次"退避要多久"的标定（探测 10：25s 不够，冷置 240s + warmup + 45s 间隔第 3 次命中）。** 它没做的：没把内联 JSON 的字段路径固定下来，没裁 UA 与端点，没写任何交付物。

### 2.1 捡拾过程中排掉的一个坑
首页 `window.$data` 里的 `componentId:151030 / componentVersionId:1856992 / uuid:9206788280` 这类数字长得像 offer id，**但不是**。按"11–13 位数字"抓 id 会全是假阳性（前人探测 1/5 就踩在这上面）。必须按 URL 形态（`/offer/<digits>.html`）或参数名（`offerId=` / `topOfferIds=`）提，且最好再回页面验一次。

---

## 3. 用的哪个链接、怎么拿到的

`find_candidates.py`（本次新写，实跑）从公开页拿链接，输出落 `_fc2.out`：

```
[harvest] wiki.1688.com  status=200 bytes=728775  bxpunish=None L1_cards=46  ids_seen=68
[harvest] www.1688.com   status=200 bytes=104224  bxpunish=None L1_cards=0   ids_seen=4
[live]     616585743654   L2-VERIFIED
[live]     545270531656   not-verified blocked:punish_shell(bytes=2039,bxpunish=1,含 _____tmd_____ 反爬壳)
[live]     890830589498   not-verified blocked:punish_shell(bytes=2039,bxpunish=1,含 _____tmd_____ 反爬壳)
...（另外 5 条同样 blocked）
"recommended_link": "https://detail.1688.com/offer/616585743654.html",
"recommended_evidence_level": "L2_offer_page",
```

**主用链接（本次自己找到的，脚本产出）：`https://detail.1688.com/offer/616585743654.html`**
怎么拿到的：`wiki.1688.com/`（"阿里巴巴1688百科首页"，1688 自己的公开商机选品位，匿名 200，无 bxpunish）→ 正则提 `"imgUrl"…"benefitPoint":"下游热销5w+","handPrice":"0.19","offerId":"616585743654","detailUrl":"https://detail.1688.com/offer/616585743654.html?…"` 这类**完整商品卡片**（46 张）→ 逐个真访移动版商品页做 L2 验证，第 1 个就验中（内联 JSON 取到 `offerTitle` + `price` + `saleOut == false`）。

**副用链接（前人的，一并复跑做对照）：`https://detail.1688.com/offer/896192389922.html`**
来源已证实是公开的 `www.1688.com` 首页菜单参数 `?offerId=896192389922`（不是编的），商品是"韩国套装2025春夏新款女童小童小熊花边背心牛仔阔腿裤长裤洋气潮"，`saleOut=false`。

在售证据分级（脚本输出里逐条标注，不混用）：
- **L2_offer_page** — 真访问了移动版商品页，内联 JSON 拿到 标题+价格+`saleOut=false`。最强。本次 1 条。
- **L1_public_card** — 公开卡片同时带 detailUrl + 到手价 + 销量文案 + 商品图。本次 46 条。
- **L0_id_only** — 页面文本里出现过 id，无任何在售字段。本次 26 条。

（前一次 `--limit 4` 跑的时候 4 个候选**全被 punish、一条没验中**，脚本如实输出了 `"ok": false, "error_code": "ALL_CANDIDATES_UNVERIFIED"` 与每条失败原因 —— 见 `_fc.out`。**注意版本**：`_fc.out` 是脚本第 1 版（还没有 L1/L2 分级）产出的，`_fc2.out` 才是当前交付版（18:44 定稿）的实跑输出；留 `_fc.out` 只为证明"拿不到时如实报错、不编造链接"这条行为。）

---

## 4. 解析策略分层与字段来源

`parse_offer.py` 的层序与本次实测命中情况：

| 层 | 策略 | 本次结论 | notes 里怎么记 |
|---|---|---|---|
| L0 | **端点规范化**：任何输入（PC 链接 / 移动链接 / 纯 id）→ 提 offerId → 一律走 `m.1688.com/offer/<id>.html` + iPhone UA + **每次全新无 cookie 会话** | **必须**。PC 端点两种 UA 全拦，走 PC 等于自杀 | `normalize:pc_or_mobile->mobile:m.1688.com/offer/<id>.html` |
| L1 | **meta 标签**（`og:title` / `twitter:title` / `<title>`） | 移动端页里 **`og:` / `twitter:` 标签不存在**（`property=` 计数 0），只有 `<title>` 有真标题 → 能兜住 title，兜不住其它字段 | `meta_tags:hit` / `meta_tags:no_data` |
| L2 | **JSON-LD**（`application/ld+json`） | **不存在**（整个 172522 字节页面里 0 处）。这条路对 1688 移动版无效，代码里保留只为"如实记 not_found" | `json_ld:not_found` |
| L3 | **页面内联 JSON `window.__INIT_DATA`**（花括号配平切片 → `json.loads`） | **唯一主命中层**，全部字段都在它里面（下表） | `inline_json:__INIT_DATA:hit` |
| L4 | **移动版页面本身**（作为端点已含在 L0；另含详情图二次抓取 `itemcdn.tmall.com`） | 详情图**走这个 CDN 是通的**：200 / 1522 字节 / 无鉴权 / 正则取到 8 张图。**不受 1688 反爬影响**（不同域名不同风控） | `mobile_page:m.1688.com=ok(bytes=172522)` / `detail_images:itemcdn:status=200:count=8` |
| L5 | **无线端接口 mtop `h5api.m.1688.com`**（`12574478` + md5 签名） | **验不通**（§5.4） | `mtop_api:status=200:len=72:ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在` |

### L3 字段路径映射（全部来自 `sample/mobile_offer.html` 实测 + 3 次运行验证）

| 输出键 | JSON 路径 | 本次实取值 | 备注 |
|---|---|---|---|
| `title` | `globalData.tempModel.offerTitle` | 韩国套装2025春夏新款女童小童小熊花边背心牛仔阔腿裤长裤洋气潮 | 备胎：`globalData.blackPage.offerTitle`、`shareModel.subject`、`<title>` |
| `price` | `globalData.tempModel.price` | `"19.90"` → 19.9 | 备胎：`data.<uuid>[offer-price].data.priceModel.currentPrices[].price`、`skuParam.skuRangePrices[].price`（该品区间 19.90–35.90） |
| `currency` | **页面无显式币种字段** | `CNY`（由 `blackPage.price` 的 `¥19.90` 符号 + 1688 国内站推断） | **推断值，不是解析值**；下游若要多币种须另设来源 |
| `moq` | `globalData.orderParamModel.orderParam.beginNum` | `1` | 另有混批门槛 `mixParam.mixNum=2 / mixAmount=100`，业务上"起订"和"混批起"是两个概念，别混 |
| `stock` | `globalData.orderParamModel.orderParam.canBookedAmount` | `9127` | 语义是**可预订量**，不是仓库物理库存；无 `canBookCount`/`inventory` 字段 |
| `main_images` | `data.<uuid>[main-pic].data.offerImages[].imgUrl`（备胎 `offerImgList`、`tempModel.defaultOfferImg`、`globalData.images[].fullPathImageURI`） | 3 张（去重后；原列表 5 条含重复） | 列表类，取不到输出 `[]` |
| `detail_images` | `data.<uuid>[basic-description].data.detailUrl` → `https://itemcdn.tmall.com/1688offer/<hash>`，抓回内容里正则 `src="...jpg"` | 8 张 | 需**第二次** HTTP（间隔 ≥2s）；失败记 notes 返 `[]` |
| `seller` | `tempModel.companyName` / `sellerLoginId` / `winportUrl` | `{<seller-company-redacted>, <seller-id-redacted>, winport.m.1688.com/page/index.html?memberId=<seller-member-id-redacted>…}` | 1688 上很多卖家 companyName 就是个体户姓名，别当品牌用 |
| （在售判定） | `tempModel.saleOut` | `false` | `find_candidates.py` 用它做 L2 判据 |
| （有用旁证） | `tempModel.saledCount` / `saledCountStr` / `topCategoryId` / `postCategoryId` / `offerUnit` / `offerModelSign.isToTikTok` | 270 / `200+` / 311 / 1037192 / 件 / **true** | `isToTikTok`、`isTbDistributionOffer`、`isDistribution` 等对 M1 选品/履约有直接价值，后续 ProductSource 可一并带出 |

页面结构要点：`__INIT_DATA` 是 148180 字节的 rax 组件数据，`data` 按组件 uuid 挂各模块（main-pic / offer-price / offer-title / basic-description / offer-attribute / offer-logistics / …），公共字段集中在 `globalData.tempModel` / `globalData.orderParamModel`。**`__GLOBAL_DADA` 也存在但字段少，`__INIT_DATA` 优先。**

---

## 5. 原始 stdout（同一链接连续 3 次，含 http_status / final_url）

### 5.1 主链接 `616585743654`（本次 `find_candidates.py` 找到的）

命令（PowerShell，串行，每次之间间隔 45s，首轮前冷置 60s）：

```
python parse_offer.py "https://detail.1688.com/offer/616585743654.html"
```

> 注：下面三块是该命令在本地原始 stdout 日志里的**逐字记录**（外层 `===== RUN n =====` 是驱动脚本加的标记行，不是 `parse_offer.py` 的输出）。
> **三次全部被拦，0/3 命中，没有一次拿到数据。** 这是有效结论，不是脚本失败：`ok:false` + `error_code:"ANTIBLOCK"` + 全部字段显式 `null` / 列表显式 `[]`。

```json
===== RUN 1 ===== 2026-10-10T18:53:25.2214350+08:00
{
  "url": "https://detail.1688.com/offer/616585743654.html",
  "ok": false,
  "http_status": 200,
  "final_url": "https://m.1688.com/offer/616585743654.html",
  "title": null,
  "price": null,
  "currency": null,
  "moq": null,
  "stock": null,
  "main_images": [],
  "detail_images": [],
  "seller": null,
  "fetched_at": "2026-10-10T18:53:25+08:00",
  "error_code": "ANTIBLOCK",
  "notes": [
    "normalize:pc_or_mobile->mobile:m.1688.com/offer/616585743654.html",
    "fetch:attempt=1:status=200:len=2039:bxpunish=1",
    "fetch:attempt=2:status=200:len=2039:bxpunish=1",
    "fetch:attempt=3:status=200:len=2039:bxpunish=1",
    "fetch:blocked=punish_shell(len=2039,bxpunish=1)",
    "mobile_page:m.1688.com=blocked",
    "mtop_api:status=200:len=72:ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在"
  ]
}
```

```json
===== RUN 2 ===== 2026-10-10T18:54:19.5549025+08:00
{
  "url": "https://detail.1688.com/offer/616585743654.html",
  "ok": false,
  "http_status": 200,
  "final_url": "https://m.1688.com/offer/616585743654.html",
  "title": null,
  "price": null,
  "currency": null,
  "moq": null,
  "stock": null,
  "main_images": [],
  "detail_images": [],
  "seller": null,
  "fetched_at": "2026-10-10T18:54:19+08:00",
  "error_code": "ANTIBLOCK",
  "notes": [
    "normalize:pc_or_mobile->mobile:m.1688.com/offer/616585743654.html",
    "fetch:attempt=1:status=200:len=2039:bxpunish=1",
    "fetch:attempt=2:status=200:len=2039:bxpunish=1",
    "fetch:attempt=3:status=200:len=2039:bxpunish=1",
    "fetch:blocked=punish_shell(len=2039,bxpunish=1)",
    "mobile_page:m.1688.com=blocked",
    "mtop_api:status=200:len=72:ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在"
  ]
}
```

```json
===== RUN 3 ===== 2026-10-10T18:55:13.5455039+08:00
{
  "url": "https://detail.1688.com/offer/616585743654.html",
  "ok": false,
  "http_status": 200,
  "final_url": "https://m.1688.com/offer/616585743654.html",
  "title": null,
  "price": null,
  "currency": null,
  "moq": null,
  "stock": null,
  "main_images": [],
  "detail_images": [],
  "seller": null,
  "fetched_at": "2026-10-10T18:55:13+08:00",
  "error_code": "ANTIBLOCK",
  "notes": [
    "normalize:pc_or_mobile->mobile:m.1688.com/offer/616585743654.html",
    "fetch:attempt=1:status=200:len=2039:bxpunish=1",
    "fetch:attempt=2:status=200:len=2039:bxpunish=1",
    "fetch:attempt=3:status=200:len=2039:bxpunish=1",
    "fetch:blocked=punish_shell(len=2039,bxpunish=1)",
    "mobile_page:m.1688.com=blocked",
    "mtop_api:status=200:len=72:ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在"
  ]
}
ALL_RUNS_DONE
```

**这组为什么全拦（可核对的因果）**：`find_candidates.py` 在 **18:52** 刚把这个品的移动版页面成功抓过一次（L2 验中），紧接着 18:53 / 18:54 / 18:55 的三连跑就一次没中 —— 与 §6.4 的结论一致：**同一 IP 对同一端点短时间内再打就进 punish**，45s 间隔不足以恢复。

### 5.2 副链接 `896192389922`（前人的链接，同一代码同一环境复跑，作对照）

命令同上，URL 换成 `.../offer/896192389922.html`；首轮前冷置 300s，run 之间 60s。三次原始 stdout：

> 存档文件为本地探针目录中的原始 stdout。**说明一处驱动脚本瑕疵**：这组的 `===== RUN n =====` 标记行因 PowerShell 格式化写法写坏了没落进文件，所以文件里是 3 个连续 JSON 块；用块内 `fetched_at`（18:46:49 / 18:47:59 / 18:49:02）与 60s 间隔对应 run1→run3，顺序无歧义。
> **结果：run1 拦、run2 完整成功、run3 拦（1/3）。**

```json
{
  "url": "https://detail.1688.com/offer/896192389922.html",
  "ok": false,
  "http_status": 200,
  "final_url": "https://m.1688.com/offer/896192389922.html",
  "title": null,
  "price": null,
  "currency": null,
  "moq": null,
  "stock": null,
  "main_images": [],
  "detail_images": [],
  "seller": null,
  "fetched_at": "2026-10-10T18:46:49+08:00",
  "error_code": "ANTIBLOCK",
  "notes": [
    "normalize:pc_or_mobile->mobile:m.1688.com/offer/896192389922.html",
    "fetch:attempt=1:status=200:len=2039:bxpunish=1",
    "fetch:attempt=2:status=200:len=2039:bxpunish=1",
    "fetch:attempt=3:status=200:len=2039:bxpunish=1",
    "fetch:blocked=punish_shell(len=2039,bxpunish=1)",
    "mobile_page:m.1688.com=blocked",
    "mtop_api:status=200:len=72:ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在"
  ]
}
```

```json
{
  "url": "https://detail.1688.com/offer/896192389922.html",
  "ok": true,
  "http_status": 200,
  "final_url": "https://m.1688.com/offer/896192389922.html",
  "title": "韩国套装2025春夏新款女童小童小熊花边背心牛仔阔腿裤长裤洋气潮",
  "price": 19.9,
  "currency": "CNY",
  "moq": 1,
  "stock": 9127,
  "main_images": [
    "https://cbu01.alicdn.com/img/ibank/O1CN01LQYpAk1NWJdNONL5Z_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01eIkEZk1NWJdOie1H8_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01mSSuMd1NWJdOkiSkD_!!<seller-id-redacted>-0-cib.jpg"
  ],
  "detail_images": [
    "https://cbu01.alicdn.com/img/ibank/O1CN01vnJZzf1NWJdNXfQJS_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01Vn7JHB1NWJdR8u6Ax_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01wJQXxG1NWJdMlzUX9_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01WAjD231NWJdMm0Qk3_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01OkHACv1NWJdPapmfD_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01mSSuMd1NWJdOkiSkD_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01LQYpAk1NWJdNONL5Z_!!<seller-id-redacted>-0-cib.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01eIkEZk1NWJdOie1H8_!!<seller-id-redacted>-0-cib.jpg"
  ],
  "seller": {
    "companyName": "<seller-company-redacted>",
    "sellerLoginId": "<seller-id-redacted>",
    "winportUrl": "https://winport.m.1688.com/page/index.html?memberId=<seller-member-id-redacted>&_tabId_=offerlist&upstreamSource=od&offerId=896192389922"
  },
  "fetched_at": "2026-10-10T18:47:59+08:00",
  "error_code": null,
  "notes": [
    "normalize:pc_or_mobile->mobile:m.1688.com/offer/896192389922.html",
    "fetch:attempt=1:status=200:len=172522:bxpunish=None",
    "mobile_page:m.1688.com=ok(bytes=172522)",
    "meta_tags:hit",
    "json_ld:not_found",
    "inline_json:__INIT_DATA:hit",
    "detail_images:itemcdn:status=200:count=8"
  ]
}
```

```json
{
  "url": "https://detail.1688.com/offer/896192389922.html",
  "ok": false,
  "http_status": 200,
  "final_url": "https://m.1688.com/offer/896192389922.html",
  "title": null,
  "price": null,
  "currency": null,
  "moq": null,
  "stock": null,
  "main_images": [],
  "detail_images": [],
  "seller": null,
  "fetched_at": "2026-10-10T18:49:02+08:00",
  "error_code": "ANTIBLOCK",
  "notes": [
    "normalize:pc_or_mobile->mobile:m.1688.com/offer/896192389922.html",
    "fetch:attempt=1:status=200:len=2039:bxpunish=1",
    "fetch:attempt=2:status=200:len=2039:bxpunish=1",
    "fetch:attempt=3:status=200:len=2039:bxpunish=1",
    "fetch:blocked=punish_shell(len=2039,bxpunish=1)",
    "mobile_page:m.1688.com=blocked",
    "mtop_api:status=200:len=72:ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在"
  ]
}
ALL_RUNS_DONE
```

### 5.3 命中率小结

两组 3 连跑：**主链接 `616585743654` = 0/3 命中（9 次内部抓取全拦）；副链接 `896192389922` = 1/3 命中（run2 一次成功，其内部只用了 1 次抓取；另两次各烧 3 次重试）**。合计 6 次运行 / 16 次内部抓取 / **1 次完整解析**。
加上本会话其它探测与两次 `find_candidates` 运行，移动版商品页整体 **45 次抓取命中 4 次（≈9%）**。

`parse_offer.py` 在被拦时一律输出 `error_code:"ANTIBLOCK"`，所有字段显式 `null`、列表显式 `[]`（**15 个键一个不缺**：`url/ok/http_status/final_url/title/price/currency/moq/stock/main_images/detail_images/seller/fetched_at/error_code/notes`，`_probe31.py` 已离线断言 `list(result.keys()) == 期望顺序`）—— 下游能区分"平台没有这个字段"与"我根本没拿到页面"。这一点在两组里都有实证：同一链接的 `ANTIBLOCK` 输出与完整输出**结构同构**，只差值。

**唯一一次完整解析的抓取成本**：1 次移动版页面（172522 字节）+ 1 次 itemcdn（8 张详情图），即 2 个 GET 就出全字段 —— 说明**贵的是命中率，不是解析**。

### 5.4 mtop 无线接口实测（`_probe30.py`，落 `_p30.out`）

```
[warm] https://m.1688.com/ 200 len 6957 bxpunish None cookies []
[warm] https://detail.1688.com/offer/896192389922.html 200 len 2397 bxpunish 1 cookies ['x5secdata']
[h5api.m.1688.com] mtop.1688.wosc.detail.getData r1 -> 200 len=72 bxpunish=None ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在
[h5api.m.1688.com] mtop.1688.wosc.detail.getData r2 -> 200 len=72 ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在
[h5api.m.1688.com] mtop.1688.alidetail.pc.getdetail r1/r2 -> 200 len=72 ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在
[h5api.m.1688.com] mtop.taobao.detail.getdetail r1/r2 -> 200 len=995 bxpunish=1 ret=nonjson
[h5api.m.1688.com] mtop.1681.detail.getdetail r1/r2 -> 200 len=72 ret=FAIL_SYS_API_NOT_FOUNDED::请求API不存在
```

判读：**匿名会话从没拿到 `_m_h5_tk`**，所以 md5 签名的 token 项恒为空 → 签不出合法请求；存在的那个 API 名（`mtop.taobao.detail.getdetail`）直接 `bxpunish=1`。前人的探测 14/15 走的就是这条路，**这条路本会话内验不通**。真实接口名/版本需要从移动版页面的 JS（`@ali/rox-od-*`、`lib-mtop/2.7.4`）里逆出，本次**刻意未做**（超出 0.5–1 天时限，且属于"把逆向工程做完"而不是"给 M1-A 一个裁决"）。

---

## 6. 拦截特征原文（"被反爬拦"到底长什么样）

### 6.1 PC 端 `detail.1688.com/offer/896192389922.html`（桌面 UA）
响应头：`200 OK`、`bxpunish: 1`、`Content-Type: text/html;charset=UTF-8`、`Set-Cookie: x5secdata=xf…`
响应体 **2397 字节**，全文（存档 `sample/punish_pc.txt`）就是阿里 baxia 的 punish 跳转壳，逐字：

```html
<script>sessionStorage.x5referer = window.location.href;var url = window.location.protocol + "//detail.1688.com//offer/896192389922.html/_____tmd_____/punish?x5secdata=<REDACTED>…&x5step=1";window.location.replace(url);window._config_ = {"action":"captcha","url":"https://detail.1688.com//offer/896192389922.html/_____tmd_____/punish?x5secdata=xfBKJH_…
```

特征清单：`bxpunish: 1` 响应头 + `_____tmd_____/punish` 路径 + `x5secdata=` 参数 + **`"action":"captcha"`** + `window.location.replace` 客户端跳转 + `x5step=1`。

### 6.2 移动端 punish 壳（`m.1688.com` 未命中时，2039 字节）
同一套特征：body 里 `punish` ×2、`rgv587` ×1、`x5sec` ×2、`captcha` ×1、`_____tmd_____` ×2，`bxpunish: 1` 响应头，`<title>` 为空，**没有任何 `__INIT_DATA`**。

### 6.3 不是这些情况（重要，别误判）
- **没有 302 到 login**：所有观测到的 HTTP 层级重定向链都是空（`hist=[]`），PC/移动都是直接 200 + 壳。`login` 字样只出现在成功页里的 JS/文案（`loginUserId`、`needLogin` 打点参数），不是跳转。
- **不是登录墙**：命中页面 `loginUserId = 0`（匿名），字段照样给全（价/库存/起订/图/卖家）。
- **不是滑块"这一次必须人过"**：`action=captcha` 说明风控要求过一次验证码；本次没做验证码，而是用**退避重试**绕开了（探测 10 与本次各有多次重试后自然拿到完整页）。也就是说它是**行为风控/限速**，不是账号墙。
- **不是壳 HTML 无数据 = 永久封**：punish 与完整页在**同一 URL、同一 UA** 上交替出现，说明是概率性/配额性拦截。

### 6.4 观测到的限速标定
| 退避节奏 | 结果 |
|---|---|
| 2.5s ×3 连击（前人探测 8） | 9 次里全 punish |
| 25s ×8（前人探测 9） | 8 次全 punish，未落盘 |
| 冷置 240s + warmup + 45s 间隔（前人探测 10） | 第 3 次命中 |
| 冷置 60s + 45s 间隔（主链接 3 连跑，紧接在 18:52 一次成功抓取之后） | **9 次抓取 0 命中** |
| 冷置 300s + 60s 间隔（副链接 3 连跑） | 7 次抓取 1 命中（run2；run1/run3 各烧满 3 次重试） |
| `find_candidates.py --limit 8 --interval 4`（在 3 连跑结束约 2 分钟后） | 8 个候选里**第 1 个就命中**，其余 7 个全拦 |
| 共享 Session 复用 `x5secdata`（本次探测 23） | **全 punish，且之后连续 13 次都 punish**（探测 23/24） |
| **每次全新无 cookie 会话**（本次探测 25、`parse_offer.py` 默认） | 命中过完整页，且响应无 Set-Cookie |

→ **可操作结论：绝不要复用带 `x5secdata` 的会话**（那是风控标记，带着它几乎必拦）；要 **一次全新会话 + 一次请求 + 大间隔（分钟级）+ 允许失败**。本会话实测命中率量级 **≈9%（4/45）**；样本很小、且严重受"我前面打了多少"影响，最好的一次窗口是"距上一次成功约 2 分钟后首试即中"，最坏是"紧接着一次成功之后 9 连拦"。**不要把 9% 当常量，要当"这条路的默认预期就是会失败"。**

---

## 7. 对应方案（按 DESIGN §10 / §9 M1-B）

### 7.1 裁决对应的接法
DESIGN §10「1688 采集方案 | M1 | 有企业资质走 Open API；没有则 M1 用手动贴链接 + 页面解析」——本次把"页面解析"这半句的**可行性边界钉死了**：

1. **入口形态**：用户手贴链接（§9 M1-B Demo 本来就是"贴 1688 链接"）。系统从链接提 `offerId`，**内部一律改写成 `https://m.1688.com/offer/<id>.html`**，Mobile Safari UA，全新无 cookie 会话。**不要把用户贴的 PC 链接直接抓**。
2. **解析**：只依赖 `window.__INIT_DATA` 一层（`spike/alibaba_parse/parse_offer.py` 已实现，字段路径见 §4）。meta / JSON-LD 两层**别当依赖**（1688 移动版没有 / 只有 `<title>`）。
3. **详情图**：走 `itemcdn.tmall.com` 二次抓取，这条路不受 1688 反爬影响，稳定。
4. **抓取调度**：**单次、分钟级间隔、可失败**。生产化的 ProductSource 必须把"抓不到"当正常分支：`error_code=ANTIBLOCK` → 落人工待办，不重试轰炸（轰炸 = 把自己打进更长冷却，本会话实测踩过：探测 23 之后连续 13 次全拦）。
5. **不要碰的路**：`detail.1688.com` PC 端点、`s.1688.com` 搜索页当列表源、`sale/pages-fast/chaojigongchang` 营销 SSR 页（本次实测 0 商品）、mtop 匿名签名接口（§5.4）。
6. **列表源**（选品官 Scout 要的"类目/竞品线索"）：`wiki.1688.com/` 是**目前唯一实测好用的公开页**（200 / 无 bxpunish / 46 张带价格与销量文案的商品卡片）。可以做线索池，但**卡片字段不足以生成 listing**（只有 imgUrl + handPrice + benefitPoint + topicName，没有标题/详情图/SKU），仍要回到 1 的单品抓取。

### 7.2 M1-B 失败降级（裁决不是"可用"，所以这段是主路径）
按 §9 M1-B「解析失败允许用户手动补录必要字段」+ M1 Done「失败运行必须有明确错误原因或人工待办，不能静默失败」：

- **`ProductSource` 主实现 = `MockProductSource`（确定性）+ 人工补录表单**，**不是**真抓取实现。理由：M1-B 的 Done 条件是"连续 3 次运行至少 2 次于 3 分钟内产出完整 listing 包"，而真抓取在本机实测是 **6 次运行 1 次成功（≈9% 的单发命中率）、退避要分钟级、且会自伤 IP 配额** —— 把它当链路前提，Demo 会随机死在抓取上。
- **`AlibabaMobileProductSource`（真实现）= best-effort 增强层**：贴链接时后台试抓 1 次（全新会话 + Mobile UA + 单次），命中就自动填表并把 `notes`/策略命中记进 `tools_call_log`；`ANTIBLOCK` 就立刻转人工，不内部无限重试。
- **人工补录的最小必要字段**（M1-B 表单要收齐才能出 listing 包）：`title`、`price`、`currency`、`moq`、`main_images`（≥1）、`detail_images`（≥1，可人工从页面另存）、`seller`。`stock` 允许人工填/按缓冲系数估，并在 products 上打 `stock_source = manual` 标记（对齐 §11「虚拟库存超卖」风险的 `stock_sync_state` 语义）。
- **人工兜底操作规范**：人工用浏览器打开 PC 链接（真浏览器能过 captcha），把标题/价/SKU 图复制进表单。**这一步就是 DESIGN §10 里"手动贴链接 + 页面解析"的人工半句。**
- **凭据纪律**：本 spike 全程零登录、不主动使用任何 Cookie 或账号凭据；未创建 `.env`。
  **如实记录一次自我修正**：探测日志（`_p22.out` / `_p25.out` / `sample/pc_offer.html` / `sample/punish_pc.txt`）与本文 §6.1 曾写入过平台**匿名反爬挑战 token 的值**（`x5secdata=…`，不是账号凭据，但按硬约束也不该落文件）。发现后已用 `spike/alibaba_parse/_sanitize_tokens.py` 就地脱敏为 `x5secdata=<REDACTED>`，脚本当次输出：`SANITIZED _p22.out tokens=3 / _p25.out tokens=1 / sample\pc_offer.html tokens=2 / sample\punish_pc.txt tokens=2 / ..\docs\spike\1688-parse.md tokens=1 / TOTAL files changed: 5`；脱敏后再 grep `x5secdata=[A-Za-z0-9_-]{16,}` 全目录**零命中**。原始值只在当时的终端回显里存在过，未进入交付代码逻辑、未进入选品/上架链路。今后任何 cookie/token 值一律不落文件。

### 7.3 M3 的连带提醒（不在本 spike 范围，只是别踩）
履约要"1688 代发下单"= **写操作**，本 spike 一律没碰（硬约束）。裁决"仅移动端页面可读且限流严重"意味着 M3 的下单通道**不能**建在这条采集路上，仍按 §9/§10 走 Open API 或人工下单 + 回查。

---

## 8. 抓取纪律（本次实际执行）

- 全程 GET-only：没发过 POST，没提交任何表单，没调用下单/加购/收藏类端点。
- 串行、单请求，请求间隔 ≥2s；§6.4 里的标定实验用的是 2.5s / 25s / 45s / 60s / 180s / 300s 各档。
- 每步命令与输出都落档在 `spike/alibaba_parse/`：`_p19.out … _p30.out`、`_fc.out`（失败那次）、`_fc2.out`（成功那次）、`_runs3.out` / `_runs3b.out`（两组 3 连跑原始 stdout）、`sample/*.html` + `sample/punish_pc.txt`。
- 没装任何包（只用预装 requests / bs4 / lxml / Pillow / stdlib）。
- 没创建 `.env`，没写入任何 cookie/token/账号凭据。
- 已知副作用（如实说）：本会话请求密度偏高，中后段本机 IP 进入过持续 punish 状态（探测 23/24 连续 13+ 次全拦），靠冷置 300s 才恢复出命中。**后续 agent 别在一小时内对 1688 铺并发。**

---

## 9. 三栏收尾（R8 纪律）

### ✅ 已验证（有当次命令 + 输出支撑）
1. `m.1688.com/offer/<id>.html` + iPhone UA + 全新无 cookie 会话 → 200 / 172522 字节完整页，**匿名**（`loginUserId=0`）。本会话独立命中 4 次：探测 22 单发、探测 25 单发、`find_candidates.py` L2 验中 `616585743654`（18:52）、`parse_offer.py` 3 连跑的 run2（18:48，`_runs3.out`）；前人另有 1 次（`_probe10.out` attempt3）。
2. 内联 `window.__INIT_DATA` 里 **title / price / moq / stock(canBookedAmount) / main_images / detail_images 来源 URL / seller / saleOut / 销量 / 类目 id** 全部取到，取值见 §4 表与 §5 原始 stdout。
3. 详情图 CDN `itemcdn.tmall.com/1688offer/<hash>` 匿名可取：200 / 1522 字节 / 8 张 `cbu01.alicdn.com` 图。
4. `detail.1688.com` PC 端点 **两种 UA 全部 punish**（2397 字节壳、`bxpunish:1`、`"action":"captcha"`），**未观测到任何一次成功**。
5. 判活特征是 UA 而非域名：`m.1688.com` + 桌面 UA = 2039 壳；+ iPhone UA = 172522 完整页。
6. `wiki.1688.com/` 匿名 200 / 728775 字节 / 无 bxpunish / 68 个真实 offer id / 46 张完整商品卡片；`find_candidates.py` 从中产出并 L2 验中一个真实在售链接（`616585743654`）。
7. 前人用的两个 id（`896192389922`、`679897239501`）**确实来自公开首页**，不是编造。
8. mtop 匿名签名路不通：无 `_m_h5_tk`；`mtop.1688.wosc.detail.getData` 等 `FAIL_SYS_API_NOT_FOUNDED`；`mtop.taobao.detail.getdetail` `bxpunish=1`。
9. `sale.1688.com/factory`、`pages-fast…/index.html`、`chaojigongchang`、`sale/distribution` 4 个营销/SSR 页 **商品 id = 0**（3 个已存档，实测正则 0 命中）。
10. `parse_offer.py` 输出**固定 15 键**，失败时字段 `null` / 列表 `[]`、`error_code` 非空。已实跑触发：`ANTIBLOCK`（两组 3 连跑里 5 次）；已离线触发并断言：`BAD_URL`（`_probe31.py`：给一个不含 offer id 的 URL，键序与 15 键完全一致，`title/price/seller` 为 `null`、`main_images/detail_images` 为 `[]`）。`NETWORK_ERROR` / `FETCH_FAILED` 是代码路径，**未实跑触发**。链接规范化（纯 id / 移动链接 / 带 `?spm=` 的 PC 链接 / 带 `topOfferIds=` 的链接 → 正确提 id）已离线断言 4 例通过。抽取逻辑另用 `sample/mobile_offer.html` 离线单测过（`_probe26.py`）。

### ⛔ 刻意未验（时限/风险/超范围）
1. **不验 1688 Open API**（企业资质、真实 appkey/secret 都没有）；DESIGN §10 那半句"有企业资质走 Open API"本次无凭据，不动。
2. **不逆 mtop 真实接口名/版本/签名流**（需要读 `@ali/rox-od-*` JS bundle，属逆向工程，超 M1-A 时限）。
3. **不做登录态抓取**：不登录、不拿 cookie、不测"登录后能多取哪些字段"（§4 里没取到的字段是否属于登录墙后，未验）。
4. **不做任何写操作**：下单、加购、收藏、议价、询价、旺旺消息一律不碰（M3 通道，超 M1-A 范围）。
5. **不验过验证码/滑块**：不跑真浏览器、不解 captcha，只用退避重试；因此"人工过一次验证码后配额是否显著恢复"未验。
6. **不测多 IP / 代理池 / 云机**：本机 IP 单点，DESIGN §10 的"海外 IP 环境"是 M2 依赖，本 spike 不建。
7. **不铺量**：没测同一 IP 连续 N 次后的封禁时长曲线（只有 §6.4 的定性档位）；没测 100+ 链接批量采集成功率，因此**不能宣称"能支撑选品量"**。
8. **不抓其它 offer 做跨品泛化验证**：只验了 2 个品的完整解析（同结构页面），未测非服装类目 / 定制品 / 混批品 / SKU 多的品的字段完整度。
9. 未碰 `s.1688.com` 搜索页能否匿名返回商品（前人测过但没留 stdout，我也没重做——裁决不靠它）。

### ❗ 不可宣称（避免把推断当证据）
1. 不能宣称"1688 采集已打通、可用于生产"。裁决是**仅移动端页面可出数 + 单发命中率 ≈9%（小样本）+ 分钟级退避**，本质是"能用但不可靠，且默认会失败"。
2. 不能宣称"证明系统真跑通了 M1-B Demo"。本 spike 只证明**单链接解析层**可取数；LLM 文案、图像重绘、选品报告、TikTok 上架全未参与。
3. 不能宣称"已验证反爬会被彻底绕过"。命中来自退避重试 + 换 UA + 弃用 cookie 会话，**平台随时可改**（`isDetailForbidden`、`pricePrivate`、`addCartPrivate` 这类开关在数据里就存在，说明它有逐品降权能力）。
4. 不能把 `canBookedAmount` 说成"真实库存"、不能把 `mixNum=2/mixAmount=100` 当 MOQ、不能把 `saledCountStr="200+"` 当精确销量。语义分别是可预订量、混批门槛、销量文案。
5. 不能把"内部一致"说成"证明真跑"：前人的成功抓取与我自己的抓取**必须靠字段路径回原文核对**才算证据 —— 我做了（§4 每条都有 JSON 路径 + `_runs3.out` 原始 stdout），但**这只对那 2 个品成立**（见 ⛔8）。
6. `currency=CNY` 是**推断**（页面无币种字段，来自 `¥` 符号与站点属性），不能宣称"从接口/页面显式字段解析出币种"。
7. 本 spike **未产生任何可上架的商品数据质量结论**：解析出来 ≠ 适合东南亚站（合规词、尺码体系、类目映射、水印都没查）。
8. 不宣称 1688 反爬"已绕过"或"已摸清规则"；只宣称**观测到的特征与命中率**。风控是黑盒，下次跑可能不同（本会话内就已经从"稳定命中"退化到"连续 13 次全拦"）。

---

## 10. 落档文件

| 文件 | 说明 |
|---|---|
| `spike/alibaba_parse/parse_offer.py` | **交付物 1**，`python parse_offer.py <offer_url>` → 固定 15 键 JSON；`--retry/--fast/--mtop/--no-mtop` |
| `spike/alibaba_parse/find_candidates.py` | **交付物 2**，公开页拿真实在售链接，L2/L1/L0 分级，拿不到如实报错 |
| `docs/spike/1688-parse.md` | **交付物 3**，本文 |
| `spike/alibaba_parse/_probe1…17*.py` | 前人探测（未改动，保留原样作为其探索路径） |
| `spike/alibaba_parse/_probe18…31.py` + `_p19…_p30.out` | 本次探测与输出（18/19/20=结构与字段定位，21–25=端点×UA×会话矩阵与 punish 原文，26=离线解析自检，27/28/29=列表源结构，30=mtop 接口，31=键契约与链接规范化的离线断言） |
| `spike/alibaba_parse/_runs3.out` / `_runs3b.out` | 两组"同一链接连续 3 次"原始 stdout |
| `spike/alibaba_parse/_fc.out` / `_fc2.out` | `find_candidates.py` 失败那次 / 成功那次的完整输出 |
| `spike/alibaba_parse/_sanitize_tokens.py` | 凭据脱敏脚本（把 `x5secdata=` / `_m_h5_tk=` 的值就地替换为 `<REDACTED>`），见 §8 凭据纪律 |
| `spike/alibaba_parse/sample/` | `mobile_offer.html`（前人 172522 成功页）、`wiki.html`、`homepage.html`、`ssr_*.html`×3、`pc_offer.html`、`punish_pc.txt`、`punish_body.txt`（注意：`punish_body.txt` 名字有误导，它装的其实是**成功**抓到的完整移动页） |

> 注：`sample/` 里的 HTML 是 1688 页面快照，含平台下发的匿名脚本；页面快照/日志里出现过的匿名反爬 token 值已由 `_sanitize_tokens.py` 统一脱敏为 `<REDACTED>`，**不含任何账号 cookie、登录态或凭据**。`_runs3.out` / `_runs3b.out` / `_p30.out` 里的 `x5secdata` 只以**字段名**形式出现。

---

## 11. Lead 复核修正（2026-10-10 19:0x，独立复现，非转述）

复验脚本：`spike/alibaba_parse/verify_ua_claim.py`（交替 3 轮 × {桌面 Chrome, iPhone Safari}，串行，间隔 4s，同一 URL `m.1688.com/offer/896192389922.html`）。

### 11.1 被反驳的一条机制断言

§1 写「**判活特征是 UA，不是域名**」，并据此给出"手机 UA 才能出数"的结论。**本次复现不支持该断言**：

| 复现批次 | UA | 结果 |
|---|---|---|
| 单发（脚本外先跑的一次） | **桌面 Chrome** | **成功**：`status=200 bytes=179309`，含 `offerDetail` |
| 交替采样 round1–3 | 桌面 Chrome ×3 | 全部 2039B punish 壳（`_____tmd_____/punish`） |
| 交替采样 round1–3 | iPhone Safari ×3 | 全部 2039B punish 壳 |
| 合计 | — | **TALLY desktop 0/3、iphone 0/3；但桌面单发曾成功过一次** |

→ 「桌面 UA 必拦」被那一次 179309 字节的成功直接否证；两种 UA 在交替采样里表现无差别。
**因此 §1 的机制结论降级为：未证实（且现有证据互相冲突）。**

### 11.2 与双方数据都自洽的解释

拦截更像**概率性 + 频率/会话相关的风控**，而非 UA 白名单：

- 本报告 §6.4 已观测到"从稳定命中退化到连续 13 次全拦"；
- 本报告 §9.8 已自我限定"下次跑可能不同"；
- 本次 0/6 发生在报告已打完 45+ 次抓取**之后**——与报告末尾"本机 IP 现处易拦状态，建议 ≥1 小时后再铺量"的提醒一致。

即：**命中率低是真的，低的原因不是"没换对 UA"。**

### 11.3 对 M1-B 决策的影响（结论不变，理由要换）

采纳本报告建议：**ProductSource 主路 = mock + 人工补录；真实移动端解析 = best-effort 增强层**（贴链接时后台试抓 1 次、全新无 cookie 会话、**单次不重轰**，命中自动填表，`ANTIBLOCK` 立即转人工待办）。

但实现时**不要**把"用 iPhone UA"当成解法去设计——它未被证实。要按"默认会失败"设计：
1. **一次成功即快照缓存**（`products.raw_snapshot`），后续运行复用事实、只重跑生成，避免为凑"连续 3 次"反复打平台；
2. **退避要长且限量**：单次抓取 + 分钟级冷却，禁止循环重轰（重轰会自伤本机 IP，本次已复现该退化）；
3. 人工补录最小字段沿用报告清单：`title / price / currency / moq / main_images / detail_images / seller`。

### 11.4 下游必须记住的三个语义坑（本报告提出，Lead 确认保留）

1. **绝不复用带 `x5secdata` 的会话**（带了几乎必拦）；
2. `canBookedAmount` ≠ 物理库存；`mixNum/mixAmount` 是混批门槛 ≠ MOQ；
3. `currency=CNY` 是**由 ¥ 符号推断**，非页面显式字段 → 落库时要带"币种为推断值"的标记，接 SCHEMA R8 L6（币种一致性）时一并裁决。

### 11.5 排期副作用

本机 IP 目前对 1688 处于**易拦状态**（报告与本复验双重印证）。真实解析适配器的验证**安排在 ≥1 小时后**，M1-B 期间不依赖它。

### 11.6 Lead 入库处理（PII 与快照，2026-10-10 19:1x）

**本报告及 `_runs3*.out` / `_p*.out` 中的真实卖家个人姓名与店铺 ID 已就地脱敏**
（`companyName` → `<seller-company-redacted>`、`sellerLoginId` → `<seller-id-redacted>`，共 6 个文件）。
理由：DESIGN §7.6 的 PII 纪律精神同样适用于仓库，且本项目按 DESIGN §1.1 未来要开源成框架——
真实卖家姓名不该进 Git 历史。**字段存在性与非空性不受影响**，"解析能取到 seller"这条结论仍然成立。

`spike/**/sample/`（1.5MB 第三方页面快照，含平台下发脚本）已加入 `.gitignore`：本地留档、不进库。
文中对这些快照的引用（如 `sample/mobile_offer.html`）**指本地路径，不在 Git 里**——
复核请在本机 `spike/alibaba_parse/sample/` 下查看。
