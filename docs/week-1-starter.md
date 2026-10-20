# 示例：本地框架起步清单（example）

> 这是合成环境的试用顺序，不是店铺第一周运营计划；不含真实订单、政策、销售目标或账号配置。能力边界见 [本地 mock 工作流](./thailand-home-workflow.md) 和 [DESIGN.md](./DESIGN.md)。

## 1. 在本地启动

1. 按 [README.md](../README.md) 安装 Python 依赖，保持 `WORKFLOW_PLATFORM=mock`、`WORKFLOW_LLM_PROVIDER=mock`、`TTS_WRITE_ENABLED=0`。
2. 启动本地服务，确认 `/healthz` 返回成功；需登录的后台请用本地管理员账号。不要在仓库里写任何真实密钥。
3. 运行 `python -m shopkeeper.cli demo-orch`，查看 `artifacts/demo-orch/` 中的模拟 run。这个结果不是外部平台验收。

## 2. 核对合成事实与审批

阅读 [synthetic-workflow.json](../examples/synthetic-workflow.json)：其中 `listing` 是草稿、`approval` 是待审，`order` 是合成只读快照。文件是结构示意，不含买家身份，不能直接发给 TikTok 或写入真实数据库。运行 mock 演示时，检查审批挂起与后续模拟步骤；任何生产发布仍需要单独授权、平台凭据及人工审批。

## 3. 使用通用提示词

[模板索引](../prompts/README.md) 给出人工输入模板。只用自造商品和空白政策字段试跑；信息不足时必须输出缺失说明，不要替使用者编价格、履约承诺或订单状态。达人建联由编排器读取的系统提示还会附加代码中的安全要求与 JSON 契约。

## 4. 验证发布候选

```bash
python scripts/check_links.py
python -m pytest tests/test_site_profile.py tests/scout/test_pipeline_category_fit.py tests/orchestrator/test_outreach_step.py -m "not real"
```

上述命令只覆盖本地链接和相关离线逻辑。第三方平台、真实商店以及面向公众的文件白名单，需要在无旧历史的新仓导出后另行复核。
