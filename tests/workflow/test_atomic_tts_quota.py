"""上架配额：并发占位、失败释放和未知结果保留。全部使用 mock HTTP。"""
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx
import pytest

from tests.workflow.test_tiktok_live_gate import _client_pair, _live_handler


def _payload(draft_id):
    return {"draft_id": draft_id, "input": {"category_id": "600001", "product": {
        "title": "Storage box", "description": "Storage box made of PP",
        "category_id": "600001", "main_images": [{"uri": "fixture-image"}],
        "skus": [{"price": {"amount": "99", "currency": "THB"},
                  "inventory": [{"warehouse_id": "fixture", "quantity": 1}]}],
    }}}


@pytest.mark.parametrize("separate_clients", [False, True])
def test_concurrent_creates_share_atomic_daily_limit(tmp_path, separate_clients):
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    writes = []
    fallback = _live_handler()

    def handler(request):
        if request.url.path.endswith("/products") and request.method == "POST":
            with lock:
                writes.append(1)
                first = len(writes) == 1
            if first:
                entered.set()
                assert release.wait(10)
            return httpx.Response(200, json={"code": 0, "data": {"product_id": f"p-{len(writes)}"}})
        return fallback(request)

    _, first, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(handler))
    second = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(handler))[1] if separate_clients else first
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(first.create_listing, _payload("a"))
            assert entered.wait(10)
            b = executor.submit(second.create_listing, _payload("b"))
            try:
                second_result = b.result(timeout=10)
            finally:
                release.set()
            first_result = a.result(timeout=10)
        assert first_result.ok
        assert not second_result.ok
        assert second_result.error.code == "TTS_WRITE_QUOTA_EXCEEDED"
        assert len(writes) == 1
        assert first.quota_status()["used"] == 1
    finally:
        release.set()
        first._client.close()
        if second is not first:
            second._client.close()


def test_before_create_failure_releases_reservation(tmp_path):
    fallback = _live_handler()
    calls = []

    def handler(request):
        if request.url.path.endswith("/attributes") and not calls:
            calls.append(1)
            return httpx.Response(200, json={"code": 999, "message": "category unavailable"})
        return fallback(request)

    _, platform, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(handler))
    try:
        assert not platform.create_listing(_payload("a")).ok
        assert platform.quota_status()["used"] == 0
        assert platform.create_listing(_payload("b")).ok
    finally:
        platform._client.close()


def test_unknown_create_result_retains_quota_after_restart(tmp_path):
    fallback = _live_handler()
    calls = []

    def handler(request):
        if request.url.path.endswith("/products") and request.method == "POST":
            calls.append(1)
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return fallback(request)

    _, platform, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(handler))
    try:
        assert not platform.create_listing(_payload("a")).ok
        assert platform.quota_status()["used"] == 1
        _, restarted, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(handler))
        try:
            denied = restarted.create_listing(_payload("b"))
            assert not denied.ok
            assert denied.error.code == "TTS_WRITE_QUOTA_EXCEEDED"
            assert len(calls) == 1
        finally:
            restarted._client.close()
    finally:
        platform._client.close()


def test_other_client_success_is_counted_after_local_precreate_failure(tmp_path):
    fallback = _live_handler()
    failed_once = []

    def first_handler(request):
        if request.url.path.endswith("/attributes") and not failed_once:
            failed_once.append(1)
            return httpx.Response(400, json={"code": 12345, "message": "fixture category error"})
        return fallback(request)

    _, first, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(first_handler))
    _, other, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(fallback))
    try:
        assert not first.create_listing(_payload("a")).ok
        assert other.create_listing(_payload("b")).ok
        assert first.quota_status()["used"] == 1
        denied = first.create_listing(_payload("c"))
        assert not denied.ok
        assert denied.error.code == "TTS_WRITE_QUOTA_EXCEEDED"
    finally:
        first._client.close()
        other._client.close()


def test_explicit_platform_rejection_releases_reservation(tmp_path):
    fallback = _live_handler()
    attempts = []

    def handler(request):
        if request.url.path.endswith("/products") and request.method == "POST":
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(400, json={"code": 12345, "message": "payload rejected"})
        return fallback(request)

    _, platform, _ = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher", transport=httpx.MockTransport(handler))
    try:
        assert not platform.create_listing(_payload("a")).ok
        assert platform.quota_status()["used"] == 0
        assert platform.create_listing(_payload("b")).ok
    finally:
        platform._client.close()
