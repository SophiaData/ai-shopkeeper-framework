# 站点画像与公开示例（P3-02）

> 产品权威：[DESIGN.md](./DESIGN.md) §1.2 / §12.1；公开快照边界见 [M4-open-source-assessment.md](./M4-open-source-assessment.md)。
> 状态：本仓为公开框架快照，示例文本与 MIT 许可已就位。

## 当前运行时事实

- `src/shopkeeper/site_profile.py` 只登记 `th-home`：TH、THB、`Asia/Bangkok`、家居收纳。`SITE_PROFILE` 未设置等同这张画像，未知 id 拒绝；`Settings.site` 仍独立默认 `SEA`，不是多站点运行开关。
- 展示字符串读取 `active_site_profile()`；调度、利润、预算和配额等业务日读取 `business_tz()`；运行时币种显示及比较读取 `profile_currency()`。库内仍为 THB satang，`workflow/migrate.py` 的 `CHECK (currency = 'THB')` 未放宽；广告只读客户端和入库缺省保留 THB 约束。
- `scout/category_rules.py` 有泰国家居收纳参考价带，偏离只提醒。该类目与 THB 约束未迁移为多站点配置；本快照不改变售价公式、迁移、时区钟点、广告预算或写闸门。
- `site_profile.HARDCODE_ANCHORS` 只登记仍在运行时硬编码的选品与 THB 调用点。同路径 [example 工作流](./thailand-home-workflow.md)、[example 起步](./week-1-starter.md) 和 [通用提示词](../prompts/README.md) 是合成资料，不属于运行时锚点。
- `orchestrator/outreach.load_system_prompt()` 可读取 `prompts/influencer-outreach.md` 的系统提示，但始终附加内置安全限制与 JSON 字段格式；其他 Markdown 模板供人工使用，短视频步骤不解析该文件。

## 公开示例与许可

`LICENSE` / `pyproject.toml` 使用 MIT，内置 Noto 字体仍遵循 SIL OFL。[合成样例](../examples/synthetic-workflow.json) 仅用虚构 ID、商品和金额说明订单/listing/审批关系，不包含真实买家身份，也不是可直接提交的 API 载荷。`TTS_WRITE_ENABLED` 默认关闭；有外部副作用的动作必须通过 `approvals`。

## 验证入口

```bash
python scripts/check_links.py
python -m pytest tests/test_site_profile.py tests/scout/test_pipeline_category_fit.py tests/orchestrator/test_outreach_step.py -m "not real"
```

离线测试不能证明真店、真 API 或多币种可用；平台、Docker、Windows 等未在本环境验证的项目应单独记录为未验。
