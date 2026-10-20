PYTHON ?= python3
export PYTHONPATH := src

.PHONY: dev up check demo demo-orch test-real

dev:
	$(PYTHON) -m shopkeeper.workflow.serve

up:
	test -f .env || cp .env.example .env
	docker compose up -d --build

check:
	# tests/workflow 含 test_seller_e2e.py：TestClient 走完草稿、403 和本地报表。
	$(PYTHON) -m pytest tests/workflow tests/adapters/test_tiktok_shop.py tests/compliance/test_lexicon.py -q -m "not real"

test-real:
	# 真 API 只读集成（不进默认 CI）。缺凭据 skip。说明见 docs/REAL-TESTS.md。
	$(PYTHON) -m pytest -m real tests/real -v

demo:
	# 离线 Demo：夹具链接 → 选品报告 + EN/TH/VI 文案 + 包内商品图。不发店铺。
	$(PYTHON) -m shopkeeper.cli demo --mock --url "https://detail.1688.com/offer/600123456789.html" --out artifacts/demo

demo-orch:
	# 编排器 mock 端到端：选品→定价→文案/图→QA→审批→上架→订单→代发→物流→客服。
	# 强制 WORKFLOW_PLATFORM=mock / WORKFLOW_LLM_PROVIDER=mock / TTS_WRITE_ENABLED=0；不外呼。
	$(PYTHON) -m shopkeeper.cli demo-orch --out artifacts/demo-orch
