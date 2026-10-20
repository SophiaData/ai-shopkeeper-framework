"""ImageStore：本地去重 + S3 骨架（httpx MockTransport，不外网）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from shopkeeper.adapters.image_store import (
    LocalFileImageStore,
    S3CompatibleImageStore,
    S3StoreCredentials,
    _aws_sign_v4,
    build_image_store,
    content_addressed_key,
    image_store_backend_from_env,
    s3_credentials_from_env,
)
from shopkeeper.adapters.tiktok_shop import TikTokCredentials, TikTokShopClient
from shopkeeper.security.error_codes import REGISTRY

JPEG_TINY = b"\xff\xd8\xff\xd9"


def test_content_addressed_key_stable_and_dedup_path():
    key1 = content_addressed_key(JPEG_TINY, filename="a.jpg")
    key2 = content_addressed_key(JPEG_TINY, filename="b.jpg", content_type="image/jpeg")
    assert key1 == key2
    digest = hashlib.sha256(JPEG_TINY).hexdigest()
    assert key1 == f"{digest[:2]}/{digest}.jpg"


def test_local_put_deduplicates_and_resolve_open(tmp_path: Path):
    store = LocalFileImageStore(tmp_path / "image_store")
    first = store.put(JPEG_TINY, filename="main.jpg", content_type="image/jpeg")
    assert first.ok and first.data is not None
    assert first.data.deduplicated is False
    assert first.data.backend == "local"
    assert first.data.byte_size == len(JPEG_TINY)
    on_disk = tmp_path / "image_store" / first.data.key
    assert on_disk.is_file()
    assert on_disk.read_bytes() == JPEG_TINY
    assert first.data.uri == on_disk.resolve().as_uri()

    second = store.put(JPEG_TINY, filename="again.jpg")
    assert second.ok and second.data is not None
    assert second.data.deduplicated is True
    assert second.data.key == first.data.key

    resolved = store.resolve(first.data.key)
    assert resolved.ok and resolved.data is not None
    assert resolved.data.uri == first.data.uri

    opened = store.open_bytes(first.data.key)
    assert opened.ok and opened.data == JPEG_TINY


def test_local_empty_and_bad_key_and_missing(tmp_path: Path):
    store = LocalFileImageStore(tmp_path)
    empty = store.put(b"")
    assert not empty.ok and empty.error is not None
    assert empty.error.code == "IMG_STORE_EMPTY"
    assert empty.error.code in REGISTRY

    bad = store.resolve("../etc/passwd")
    assert not bad.ok and bad.error is not None
    assert bad.error.code == "IMG_STORE_BAD_KEY"

    missing = store.open_bytes("aa/" + "0" * 64 + ".jpg")
    assert not missing.ok and missing.error is not None
    assert missing.error.code == "IMG_STORE_NOT_FOUND"


def test_backend_and_credentials_and_factory_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert image_store_backend_from_env("local") == "local"
    assert image_store_backend_from_env("s3") == "s3"
    assert image_store_backend_from_env("minio") == "s3"
    assert s3_credentials_from_env({}) is None
    assert s3_credentials_from_env({"IMAGE_STORE_S3_ENDPOINT": "http://x"}) is None

    full = {
        "IMAGE_STORE_S3_ENDPOINT": "http://127.0.0.1:9000",
        "IMAGE_STORE_S3_BUCKET": "shop",
        "IMAGE_STORE_S3_ACCESS_KEY": "ak",
        "IMAGE_STORE_S3_SECRET_KEY": "sk",
        "IMAGE_STORE_S3_REGION": "us-east-1",
        "IMAGE_STORE_S3_PREFIX": "imgs",
    }
    creds = s3_credentials_from_env(full)
    assert creds is not None and creds.configured

    local = build_image_store(backend="local", env={"ARTIFACTS_DIR": str(tmp_path)})
    assert isinstance(local, LocalFileImageStore)

    # s3 但凭据不全 → 本地回退
    fallback = build_image_store(backend="s3", env={"IMAGE_STORE": "s3", "ARTIFACTS_DIR": str(tmp_path)})
    assert isinstance(fallback, LocalFileImageStore)

    monkeypatch.setenv("IMAGE_STORE", "s3")
    for k, v in full.items():
        monkeypatch.setenv(k, v)
    s3 = build_image_store(env={**full, "IMAGE_STORE": "s3"})
    assert isinstance(s3, S3CompatibleImageStore)


def _s3_creds() -> S3StoreCredentials:
    return S3StoreCredentials(
        endpoint="http://s3.test",
        bucket="media",
        access_key="AKIAEXAMPLE",
        secret_key="secret-example",
        region="us-east-1",
        prefix="prod",
        public_base_url="https://cdn.example/prod",
    )


def test_s3_put_get_head_with_mock_transport_no_network():
    blob = JPEG_TINY
    key = content_addressed_key(blob, filename="x.jpg")
    object_key = f"prod/{key}"
    store_box: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # 不得出现明文 secret 进 URL
        assert "secret-example" not in str(request.url)
        path = request.url.path
        assert path.endswith(f"/media/{object_key}") or object_key.split("/")[-1] in path
        assert "Authorization" in request.headers
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
        if request.method == "PUT":
            store_box[object_key] = request.content
            return httpx.Response(200)
        if request.method == "HEAD":
            if object_key not in store_box:
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={
                    "content-length": str(len(store_box[object_key])),
                    "content-type": "image/jpeg",
                },
            )
        if request.method == "GET":
            if object_key not in store_box:
                return httpx.Response(404)
            return httpx.Response(200, content=store_box[object_key])
        return httpx.Response(405)

    store = S3CompatibleImageStore(
        _s3_creds(),
        transport=httpx.MockTransport(handler),
        amz_date="20261013T082600Z",
    )
    put = store.put(blob, filename="x.jpg", content_type="image/jpeg")
    assert put.ok and put.data is not None
    assert put.data.key == key
    assert put.data.uri.startswith("https://cdn.example/prod/")
    assert put.data.backend == "s3"

    resolved = store.resolve(key)
    assert resolved.ok and resolved.data is not None
    assert resolved.data.byte_size == len(blob)

    opened = store.open_bytes(key)
    assert opened.ok and opened.data == blob


def test_s3_http_error_and_missing_credentials_codes():
    empty_creds = S3StoreCredentials(endpoint="", bucket="", access_key="", secret_key="")
    bare = S3CompatibleImageStore(empty_creds)
    missing = bare.put(JPEG_TINY)
    assert not missing.ok and missing.error is not None
    assert missing.error.code == "IMG_STORE_MISSING_CREDENTIALS"
    assert missing.error.code in REGISTRY

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    store = S3CompatibleImageStore(_s3_creds(), transport=httpx.MockTransport(boom))
    failed = store.put(JPEG_TINY, filename="a.jpg")
    assert not failed.ok and failed.error is not None
    assert failed.error.code == "IMG_STORE_HTTP_ERROR"
    assert failed.error.code in REGISTRY


def test_aws_sign_v4_deterministic_for_fixed_date():
    url = "http://s3.test/media/ab/abc.jpg"
    signed = _aws_sign_v4(
        method="PUT",
        url=url,
        headers={"content-type": "image/jpeg"},
        body=JPEG_TINY,
        access_key="AKID",
        secret_key="SECRET",
        region="us-east-1",
        amz_date="20261013T082600Z",
    )
    again = _aws_sign_v4(
        method="PUT",
        url=url,
        headers={"content-type": "image/jpeg"},
        body=JPEG_TINY,
        access_key="AKID",
        secret_key="SECRET",
        region="us-east-1",
        amz_date="20261013T082600Z",
    )
    assert signed["Authorization"] == again["Authorization"]
    assert "SECRET" not in signed["Authorization"]
    assert signed["x-amz-date"] == "20261013T082600Z"


def test_tiktok_upload_image_still_blocked_by_tts_write_default():
    """ImageStore 不解锁 TTS：上传 TikTok 图片接口默认仍 TTS_WRITE_BLOCKED。"""
    client = TikTokShopClient(
        TikTokCredentials(
            app_key="k",
            app_secret="s",
            access_token="t",
            shop_cipher="cipher",
        ),
        allow_writes=False,
    )
    blocked = client.upload_image(JPEG_TINY)
    assert not blocked.ok and blocked.error is not None
    assert blocked.error.code == "TTS_WRITE_BLOCKED"


def test_all_img_store_codes_registered():
    for code in (
        "IMG_STORE_EMPTY",
        "IMG_STORE_BAD_KEY",
        "IMG_STORE_NOT_FOUND",
        "IMG_STORE_IO_ERROR",
        "IMG_STORE_MISSING_CREDENTIALS",
        "IMG_STORE_HTTP_ERROR",
        "IMG_STORE_TIMEOUT",
    ):
        assert code in REGISTRY
