# 贡献指南

本仓库以 AI 为主力开发者，规则写得很实。**先读这三份再看代码**：

| 文档 | 是什么 |
|---|---|
| [AGENTS.md](./AGENTS.md) | 常驻规则（九条）与仓库布局 |
| [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md) | 一次改动从领受到合并的完整路径 ✅ **必读** |
| [docs/CONVENTIONS.md](./docs/CONVENTIONS.md) | 写作、归属、评审、验证纪律 |

冲突时以 [DESIGN.md](./docs/DESIGN.md) / [SCHEMA.md](./docs/SCHEMA.md) 为准。

## 许可

公开框架快照的根目录 [LICENSE](./LICENSE) 为 **MIT**。内置第三方字体各自遵循 SIL OFL，通知与来源见 [docs/fonts.md](./docs/fonts.md)。本仓不包含原真店运营手册、第一周清单或调优 prompt 正文；同路径示例均为合成内容。站点画像的残余约束见 [docs/P3-02-dehardcode.md](./docs/P3-02-dehardcode.md)。

---

## 环境

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # .env 已 gitignore，永不提交
```

Windows 一键脚本见 [scripts/windows/](./scripts/windows/)（默认端口 **8010**）。

---

## 提交前必须跑

```bash
PYTHONPATH=src python -m pytest tests -m "not real"   # 离线确定性门
python scripts/check_links.py                          # 文档链接门
```

Windows 上等价入口：`powershell -File scripts/ci.ps1`（无 Docker 时加 `-SkipPg`）。

**`-m "not real"` 是默认门**：`tests/real/` 下的真实 API 测试默认不跑
（需要凭据，且会外呼），见 [docs/REAL-TESTS.md](./docs/REAL-TESTS.md)。

---

## 硬性规则

### 1. 所有改动经 PR 合并，**没有例外**

紧急修复同样开分支走 PR。**禁止**直接提交或强推 `master`。

### 2. 合并前 **3 个 CI 检查必须全绿**

`pytest (not real)` / `pytest (not real, windows)` / `desktop scaffold (no rustc)`

**不要改这三个 job 的 `name`** —— 分支保护依赖它们，改了会静默失去保护。

### 3. 小 PR、单一目的

默认 **squash 合并**。跨模块或改契约的改动才拆多个 commit（见 DEVELOPMENT §7.1）。

### 4. 一个事实只有一个家

归属见 [CONVENTIONS §1](./docs/CONVENTIONS.md)；其他位置只放链接，不复制内容。

### 5. 没有新鲜证据不得标完成

完成条件是「**命令 + 期望输出**」，贴不出输出视为未完成。

### 6. 不编造、不冒充

未接通的能力只写「计划 + 当前限制」；实测数字标明来源与时间；
推断明确标注为推断。**无法核对官方契约时不要臆造端点或字段。**

### 7. 同一次改动同步所有受影响文档

改契约、实体、状态机或 API 时，权威文档对应小节是同一次提交的义务。

### 8. 关键契约要有能「故意失败」的负向用例

不只测成功路径。**把保护拆掉，用例必须转红** —— 否则你不知道自己测到了什么。

### 9. 重大取舍先落记录

需要 owner 拍板的写进 [DESIGN §12](./docs/DESIGN.md)，不擅自改冻结文档。

---

## 绝对不能提交的东西

| 禁止 | 为什么 |
|---|---|
| `.env` | 含密钥；已 gitignore，别用 `-f` 强加 |
| API 密钥 / token / 店铺 cipher | 进 Git 历史**无法撤回** |
| 买家 PII（姓名、电话、地址、聊天） | 隐私；见 [SECURITY.md](./SECURITY.md) |
| 真实数据库文件 | 含上述全部；用 [synthetic-workflow.json](./examples/synthetic-workflow.json) 的合成形状示意 |
| 真实店铺运营手册、调优 prompt 正文与私有费率 | 本仓只收合成 example；不要把私有运营内容提交到公开仓 |
| 本机路径、审计日志、备份与数据库 | 不随公开仓分发 |
| `src-tauri/target/` | 2.5G 构建产物，已 gitignore |

密钥**只进 `.env`**。发现历史里有密钥，按 [SECURITY.md](./SECURITY.md) 报告，不要只删当前文件。

---

## 涉钱动作的红线

上架、改价、下单、投放**一律先过审批闸门**（纯读与纯生成不过闸门）。
发货、退款、付款在本仓库**始终禁用**。

**默认不解锁 `TTS_WRITE_ENABLED`。** 不要在 PR 里顺手打开它 ——
那需要 owner 书面批准，且用完立刻关（`scripts/windows/tts-disarm.ps1`）。

---

## 提交信息

用**祈使句**说明「做了什么 + 为什么」，中文即可。改契约时在正文写清影响面。
复杂改动建议附**验证命令与输出**。
