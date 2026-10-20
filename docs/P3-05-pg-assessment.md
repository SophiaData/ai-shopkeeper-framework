# 工作台 SQLite → PostgreSQL 切换评估（P3-05）

> 上游权威：[DESIGN.md](./DESIGN.md) §6 存储、[SCHEMA.md](./SCHEMA.md) ｜
> 相关：[DEVELOPMENT.md](./DEVELOPMENT.md)（双库 DDL 校验）、[P3-03-rpa-design.md(./P3-03-rpa-design.md)（同样的门槛写法）
> 状态：**评估，未实施** ｜ 不改运行时、不开迁移

---

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| 现在要把工作台切到 PG 吗？ | **不建议。** 当前单店单机场景下 SQLite 够用，切换成本与收益不成比例 |
| 两个库现在是什么状态？ | **业务库**已用 SQLAlchemy（PG 就绪）；**工作台**是**裸 `sqlite3`**，没有 PG 路径 |
| 切换工作量 | **大**：302 处 `?` 占位符、7 处 `ON CONFLICT`、2 处 `INSERT OR IGNORE`、`migrate.py` 34 处 `sqlite3` |
| 触发条件 | 见 §4：**多实例并发写** 或 **单库 > 数 GB** 或 **需要异地备份/只读副本** |
| 本文是什么 | 评估与迁移路径；**不实施** |

---

## 1. 先把「两个库」说清楚

本仓库有**两个独立的库**，容易混：

| | 业务库 | 工作台库 |
|---|---|---|
| 路径 | `DATABASE_URL` | `WORKFLOW_DB_PATH`（默认 `artifacts/workflow.db`） |
| 内容 | 订单、listing、审批等业务实体 | 草稿、报表、任务、审计、待审队列 |
| 实现 | **SQLAlchemy**（`db/engine.py`） | **裸 `sqlite3`**（`workflow/store.py`） |
| PG 就绪 | ✅ 已支持 `postgresql://` | ❌ **无 PG 路径** |
| 关系 | 两个库**互不写入**（见 `serve.py` 模块说明） | 同左 |

**P3-05 说的是「工作台」** —— 即右边那个。它没有走 SQLAlchemy，所以没有现成的 PG 开关。

---

## 2. 实测的切换成本（数字来自当前源码）

| 项 | 数量 | 说明 |
|---|---|---|
| `store.py` 行数 | 2204 | 单文件 |
| `?` 占位符 | **302** | psycopg 用 `%s`，**每一处都要改** |
| `ON CONFLICT` | 7 | 语法基本兼容，但要核对约束名 |
| `INSERT OR IGNORE` | 2 | **PG 无此语法**，要改 `ON CONFLICT DO NOTHING` |
| `rowcount` 依赖 | 14 | PG 下语义一致，但要逐个复核 |
| `migrate.py` 的 `sqlite3` | 34 | 迁移机制本身绑死 sqlite3 |
| `PRAGMA` | 4 | `busy_timeout` / `journal_mode=WAL` / `table_info` / `database_list` —— **PG 全无对应** |
| **PG 驱动** | **0（未声明）** | `pyproject.toml` 无 `psycopg`（实测 `import psycopg` 失败）；应用连 PG 需新增依赖并锁定 |

### 2.1 最麻烦的不是占位符，是这三处

**① `PRAGMA table_info` 驱动的列检测**

`_add_column_if_missing` 靠 `PRAGMA table_info` 判断列是否存在（迁移幂等的基础）。
PG 对应的是 `information_schema.columns`，**语义与权限模型都不同**。

**② `PRAGMA journal_mode=WAL`**

这是 SQLite 的并发/崩溃安全机制。PG 有完全不同的 MVCC 模型 —— 不是「换个写法」，
而是**整块删除**，同时要回答「那 PG 下的等价保证是什么」。

**③ `_db_path_of()` 依赖文件路径**

P0-10 的迁移前备份用 `PRAGMA database_list` 取库文件路径，再 `sqlite3.Connection.backup` 热备。
**PG 没有「库文件」这个概念** —— 备份要换成 `pg_dump`，且要处理连接串里的密码（不能进日志）。

> 这三处说明：**切换不是改 SQL 方言，而是改三个机制**（迁移、并发、备份）。

---

## 3. 为什么现在不值得做

| 维度 | 现在（SQLite） | 切 PG 的收益 |
|---|---|---|
| 部署 | **零依赖**，单文件 | 要跑 PG 实例（Docker 或托管） |
| 备份 | 复制文件 / 已有 `.premigrate` 热备 | 要 `pg_dump` + 凭据管理 |
| 并发 | 单机单进程够用（WAL） | 多实例并发写才需要 |
| 数据量 | 单店数据量小 | 数 GB 以上才明显 |
| 迁移风险 | —— | **302 处占位符 + 3 个机制重写** |

**本仓库当前是「单店、单机、单进程」**（`WORKFLOW_HOST=127.0.0.1`）。
没有并发写需求时，切 PG 是**纯增加复杂度**。

---

## 4. 触发条件（须**任一**满足，且满足后仍需 owner 拍板）

1. **多实例并发写** —— 例如 Windows 服务与 Docker 同时跑，或要上多副本
2. **单库超过数 GB** —— 且实测出 SQLite 是瓶颈（不是猜）
3. **需要异地备份 / 只读副本** —— PG 的流复制或托管备份是硬需求
4. **托管环境强制** —— 部署平台不提供持久化文件卷

**未满足则保持 SQLite。** 与 [P3-03(./P3-03-rpa-design.md) 同样的门槛写法，避免「顺手升级」。

---

## 5. 若真要迁移：路径

### 阶段 A：先做抽象层（**不改行为**）

把 `store.py` 里的 SQL 执行收敛到一个薄接口（`execute` / `query` / `transaction`），
**仍然只实现 SQLite**。

- 目的：把 302 处占位符集中到一处处理
- 验收：**现有测试全绿且行为不变**（这一步不该有任何功能变化）
- 出口：若抽象层无法在不改行为的前提下建立，说明代价更高，回到 §3

### 阶段 B：加 PG 实现 + 双跑测试

同一套测试**同时跑 SQLite 与 PG**。

- 关键：`-m "not real"` 离线门**两个后端都要跑**
- 起 PG 可参考既有先例：`scripts/ddl_verify.py` 用 **Docker + `psql`**（镜像
  `docker.m.daocloud.io/library/postgres:15`，容器 `PG_CONTAINER`）。`ci.ps1` 第 2 步
  跑的正是它，`-SkipPg` 会显式标注该次运行**不满足 M1-C C1**。
- **需要新增 Python PG 驱动**：当前 `pyproject.toml` **未声明** `psycopg`/`psycopg2`
  （实测 `import psycopg` 失败）。既有的 `ddl_verify.py` 走 `docker exec psql`，
  所以不需要驱动 —— 但**应用侧连 PG 必须新增依赖并锁定版本**。

### 阶段 C：迁移脚本 + 回滚

- 数据搬迁：逐表导出/导入，**带行数校验**（`COUNT(*)` 对齐）
- **回滚必须可行** —— 参考 P1-04 的思路：迁移前留完整副本，不依赖「事务回滚」
- 切回 SQLite 要能在 5 分钟内完成，否则不上线

### 阶段 D：切换

`WORKFLOW_DB_URL` 决定后端（**不删 SQLite 路径**）。
SQLite 保留为默认，PG 是显式选项 —— 与本仓库「默认保守」的一贯做法一致。

---

## 6. 与现有机制的关系

| 机制 | 当前 | 切换后需要 |
|---|---|---|
| 迁移（`migrate.py`） | `schema_version` 表 + `PRAGMA table_info` | 改用 `information_schema`；**保留版本表** |
| 迁移前备份（P1-04） | `.premigrate.vN` 热备 | 换 `pg_dump`；**连接串不能进日志** |
| 恢复后校验（P1-03） | 比对 `schema_version` | 同样适用（版本表是抽象，不依赖后端） |
| 双库 DDL 校验 | SQLite + Docker PG15 | 工作台也要纳入 |

> 好消息：**`schema_version` 表的设计与后端无关**，P1-03/P1-04 的思路可以平移。

---

## 7. 验收（本文档自身）

```bash
python scripts/check_links.py
```

期望：通过。本文**不改运行时、不开迁移、不新增依赖**。

---

## 8. 何时重新评估

- §4 的任一触发条件被满足（**附实测证据**，不是猜测）
- owner 明确要求

**在此之前工作台保持 SQLite。**
