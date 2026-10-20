# 公开框架快照说明

> 权威：[DESIGN.md](./DESIGN.md) §1.2、§9、§12.1；站点和运行时限制见 [P3-02-dehardcode.md](./P3-02-dehardcode.md)。
> 本仓是另行导出的公开框架快照，不含原私有开发仓的 Git 历史，也不含真店运营手册、第一周清单或调优 prompt 正文。

## 本仓包含什么

- 根目录 [LICENSE](../LICENSE) 与 [pyproject.toml](../pyproject.toml) 的许可标识均为 MIT；`src/shopkeeper/assets/fonts/OFL-*.txt` 和 [fonts.md](./fonts.md) 保留 Noto 字体单独的 SIL OFL 许可。
- [thailand-home-workflow.md](./thailand-home-workflow.md)、[week-1-starter.md](./week-1-starter.md) 是合成 example；[prompts/](../prompts/README.md) 的七份文件是中性模板。合成订单/listing/审批结构见 [synthetic-workflow.json](../examples/synthetic-workflow.json)。
- `python -m shopkeeper.cli demo-orch` 与 `WORKFLOW_PLATFORM=mock`、`WORKFLOW_LLM_PROVIDER=mock` 是本地可读入口。外部适配器先实现 `MockXxx` 并走同一套契约测试；真实平台调用另行验证。`TTS_WRITE_ENABLED=0` 默认锁定。
- 达人建联的 `load_system_prompt()` 若找到 Markdown，会把 example 提示与内置安全规则以及 JSON 输出契约一起交给模型；文件缺失时继续用内置规则与 JSON 契约。

## 实际能力边界

当前仓库以 TikTok Shop Thailand、家居收纳类目规则、`Asia/Bangkok` 业务日与 THB satang 为默认例子；`site_profile` 仅登记 `th-home`，库内 `CHECK (currency = 'THB')` 不放宽。Shopee CSV 只读是局部订单能力；框架没有实现通用多站点、多币种或 Shopee 写路径。客服真实收件/发送、1688 自动下单、真实首单闭环等仍有限制，以 [DESIGN §9](./DESIGN.md) 实际勾选与未勾选项为准。合成 fixture 不能当作真实平台数据或产品验收。

## 本仓不包含的内容

| 范围 | 说明 |
|---|---|
| 真店运营手册、第一周运营清单、调优 prompt 正文及其实践记录 | 只保留在私有开发仓；本仓同路径文件均为明确标记 example 的合成内容 |
| 内部评审、审计、交接与任务过程记录 | 可能包含本机路径或内部过程，不随公开快照发布 |
| 本机数据库、密钥、日志、备份、运行产物与构建缓存 | 只在本机使用，不进入公开仓 |

## 验收

1. 本地：链接门、聚焦测试、离线 pytest、前端 npm test/build，并核对入库 dist 与新构建一致。
2. 文件边界：公开文件集不包含上述私有正文、本机路径、凭据与未授权第三方材料；MIT 与字体 OFL 通知齐全。
3. 对外入口：从干净 clone 安装并以 mock 跑通演示；无法在本环境验证的平台、Docker 或 Windows 项目应单独列为未验。

示例命令通过不等于真实平台或生产验收。真实店铺写入默认关闭；启用须独立审批与显式配置。
