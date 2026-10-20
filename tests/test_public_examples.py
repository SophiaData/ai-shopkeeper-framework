"""公开候选示例清单：固定本轮审定正文，防止旧业务文本意外回填。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只固定本轮改写的同路径正文；变更示例必须重新审读全文并显式更新摘要。
EXAMPLE_SHA256 = {
    "docs/thailand-home-workflow.md": "acaa1d6dd75694e53a84a9b3db8d7a3274660987e825d87d8e2d2424b825a786",
    "docs/week-1-starter.md": "7155e4784d447fba81c7ed1143864a87ea4b523f1fd0878d97d0af1d0c5f53b5",
    "prompts/README.md": "6fb0259eb8c9c8833611d68f041937b76155210419035cc888f5c088fafd582e",
    "prompts/customer-service.md": "35553f38bc5f0e6c931a403ae9f312ac39c75b3267487d12ac02d7afc3388aa3",
    "prompts/daily-report.md": "8e505214259eb385234be6bd7d847a7b94bd03b4dea274507646d64cc66176b8",
    "prompts/faq-seed.md": "0233790d5722e88a1e8226f927854a253155b9002f822e7a001d0aa1997fc127",
    "prompts/influencer-outreach.md": "4429d235828050bc52c5af6aafb21af240f584e7b8a6bfb3ae2a5ba2367adc37",
    "prompts/listing-copy.md": "c9c5128bca0d6bc88870063ac413dd58094e653cd3910bda9ec7d2ac9b1f0586",
    "prompts/product-research.md": "bf8f696ac5ceef1391a888d27f128759562b0078a9a927265ab7945764fc0ce2",
    "prompts/short-video-script.md": "1a3b4a116820958d0e7567a631c8a7a1d02f56e9380dcc04f84b095956715ff8",
}


def test_public_example_text_matches_reviewed_candidate() -> None:
    for rel, expected in EXAMPLE_SHA256.items():
        path = ROOT / rel
        assert path.is_file() and not path.is_symlink(), rel
        text = path.read_text(encoding="utf-8")
        assert "example" in text.lower(), rel
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, rel


def test_synthetic_example_has_only_synthetic_identities() -> None:
    path = ROOT / "examples" / "synthetic-workflow.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["example"] is True and payload["synthetic"] is True
    assert payload["listing"]["id"].startswith("example-")
    assert payload["approval"]["id"].startswith("example-")
    assert payload["order"]["id"].startswith("example-")
    assert payload["order"]["buyer"] is None
    assert payload["order"]["shipping_address"] is None
    assert payload["listing"]["platform_product_id"] is None
