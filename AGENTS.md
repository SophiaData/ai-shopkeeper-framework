# AGENTS.md — 本仓库常驻指令

本项目（AI 店长 / ai-shopkeeper）以 AI 为主力开发者。本文件列出每个 AI 会话都必须知道的规则，每条一两行，规则本体链接到它的「家」。

## 权威文档（先读再动手）

| 文档 | 是什么的唯一权威 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 产品目标、架构、模块职责、里程碑 Done 条件 |
| [docs/SCHEMA.md](docs/SCHEMA.md) | 数据模型、状态机矩阵、守恒式、DDL |
| [docs/M1-PLAN.md](docs/M1-PLAN.md) | 切片拆分、完成条件、write scope、blocked_by |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 写作规则、文档归属、评审报告结构、验证纪律 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 一次改动从领受到合并的流程路径 |

冲突时以 `DESIGN`/`SCHEMA` 为准并在文档内解决。不允许代码里悄悄偏离，也不允许在新文档里另立口径。

## 常驻规则

1. **先读权威再动手**。改动设计或开始实现前，必读上表对应文档；发现矛盾回到权威文档解决，不在代码注释里发明第三种说法。
2. **一个事实只有一个家**。归属见 [CONVENTIONS §1](docs/CONVENTIONS.md)；其他位置只放链接，不复制内容。
3. **没有新鲜证据不得标完成**。完成条件是「命令 + 期望输出」，贴不出输出视为未完成（[M1-PLAN §1](docs/M1-PLAN.md)）。
4. **不编造、不冒充**。未接通的能力只写「计划 + 当前限制」；实测数字标明来源与时间，推断明确标注为推断（[CONVENTIONS §2](docs/CONVENTIONS.md)）。
5. **涉钱动作必须过审批闸门**。上架、改价、下单、投放一律先生成 `approvals` 记录；纯读与纯生成不过闸门（[DESIGN §2.1](docs/DESIGN.md)）。
6. **外部依赖 mock 先行**。每个适配器先实现 `MockXxx`，真实实现逐个替换，两者走同一套契约测试（[DESIGN §2.1](docs/DESIGN.md)）。
7. **同一次改动同步所有受影响文档**。改契约、实体、状态机或 API 时，权威文档对应小节是同一次提交的义务，不是「回头补」。
8. **重大取舍先落记录**。需要 owner 拍板的写进 [DESIGN §12](docs/DESIGN.md) 或 [M1-PLAN §0.2](docs/M1-PLAN.md)，不擅自改冻结文档。
9. **所有改动经 PR 合并，没有例外**。紧急修复同样开分支走 PR，不允许直接提交或强推 `master`；提交粒度与合并方式见 [DEVELOPMENT §7](docs/DEVELOPMENT.md)。

## 仓库布局

```
docs/                       文档（写法与归属见 CONVENTIONS.md）
  DESIGN.md                 产品设计权威
  SCHEMA.md                 数据与状态机权威
  M1-PLAN.md                切片计划与完成条件
  CONVENTIONS.md            写作、归属、评审、验证规则
  DEVELOPMENT.md            一次改动的流程路径（领任务→合并）
  spike/                    外部依赖裁决（通/不通 + 降级）
src/shopkeeper/             应用代码（单包）
  adapters/                 平台/LLM/图像/采集适配器，mock 与真实同契约
  api/                      FastAPI 路由
  db/                       SQLAlchemy 模型与仓储
  domain/                   纯函数领域逻辑（状态机、守恒式）
  scout/                    选品官
  stylist/                  listing 与图片生成
  security/                 脱敏守卫、加密、错误码
tests/                      与被测模块同构；-m "not real" 为离线确定性门
tests/real/                 真 API 只读集成（@pytest.mark.real；make test-real；见 docs/REAL-TESTS.md）
scripts/ci.ps1              本地 CI 入口，六步
scripts/windows/            中文 Windows 一键安装/启动（install.ps1、start.ps1 / start.bat；默认端口 8010）
scripts/product_research_digest.py
scripts/daily_report.py     卖家本地 CSV 摘要（不访问网络）
prompts/                    中性 example 提示词模板；达人建联的系统提示会被编排器读取
n8n/                        提醒类工作流存根（默认不启用，不含凭据）
src/shopkeeper/workflow/    泰国站工作台：只读 TikTok、草稿队列、代发草稿闸门、曼谷定时报表
src/shopkeeper/orchestrator/ AI 工作流编排器（声明式流水线含短视频脚本步与达人建联草稿步、可恢复 run、审批挂起）
src/shopkeeper/pricing/     售价公式（占位费率；编排器定价步 + /api/workflow/pricing/quote）
src/shopkeeper/gateway/     平台操作网关（local 直连 / remote 长轮询队列）
apps/desktop/               Tauri 2 桌面壳（加载本机 /workflow；说明见其 README）
```

[docs/thailand-home-workflow.md](docs/thailand-home-workflow.md) 和 [docs/week-1-starter.md](docs/week-1-starter.md) 是合成 example；原业务执行手册不进入公开快照。产品与数据口径仍以上表的 DESIGN / SCHEMA 为准。

## 验证

本地 CI：`powershell -File scripts/ci.ps1`（无 Docker 时加 `-SkipPg`，该次运行不满足 C1）。

六步：pytest（`-m "not real"`）/ 双库 DDL / 密钥扫描 / `.env` 未入版本控制 / 文档链接门 / import smoke。任一步失败退出码非零。

- 测试与代码同置，测试文件跟随被测模块。
- 关键契约要有能「故意失败」的负向用例，不只测成功路径。
- 环境差异（缺字体、缺 Docker、无 pwsh）与真实缺陷分开报告，不混入失败计数。

## 本项目的模型与外部依赖

LLM 与图像走本机 magpie 网关（`http://127.0.0.1:3425/v1`），主模型 `group/auto-deepseek-v4-flash`，图像 `workbuddy/hunyuan-image-v3.0-art`；公网备路见 `.env.example`。凭据只进 `.env`（已 gitignore），禁止进入代码、文档与 Git 历史。具体裁决与实测见 [docs/spike/](docs/spike/)。
