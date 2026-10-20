"""工作台 API 的本机或口令检查。

没设置 WORKFLOW_API_TOKEN 时，只接受回环地址（测试客户端也算本机）。
设置之后，任何来源都要带上相同的 X-Workflow-Token。
"""

from __future__ import annotations

import os
import secrets

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def workflow_api_allowed(host: str | None, token_header: str) -> bool:
    expected = os.getenv("WORKFLOW_API_TOKEN", "").strip()
    provided = (token_header or "").strip()
    if expected:
        return secrets.compare_digest(provided, expected)
    return (host or "") in LOCAL_HOSTS
