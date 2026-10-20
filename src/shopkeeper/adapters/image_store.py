"""商品图片托管（ImageStore）：本地 artifacts 默认 + S3 兼容骨架。

默认 `IMAGE_STORE=local`（或缺省）→ `LocalFileImageStore`，根目录
`IMAGE_STORE_DIR`（默认 `{ARTIFACTS_DIR}/image_store`），文件名按内容 SHA-256
命名去重。

`IMAGE_STORE=s3` **且** `IMAGE_STORE_S3_ENDPOINT` / `BUCKET` / `ACCESS_KEY` /
`SECRET_KEY` 齐全时才构造 `S3CompatibleImageStore`（httpx PutObject/GetObject
骨架，SigV4 签名）；否则回退本地，不抛、不外呼。凭据只从环境读，
`.env.example` 仅占位。

本模块**不**解锁 `TTS_WRITE_*`：把生成图上传到 TikTok 图片接口仍走
`TikTokShopClient.upload_image`，默认 `allow_writes=False` → `TTS_WRITE_BLOCKED`。
失败一律 `AdapterResult`，错误码 `IMG_STORE_*`。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

import httpx

from shopkeeper.adapters.base import ImageStore
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult, StoredImage

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "local"
S3_BACKEND = "s3"
DEFAULT_STORE_SUBDIR = "image_store"
DEFAULT_S3_REGION = "us-east-1"
DEFAULT_CONTENT_TYPE = "application/octet-stream"

_SECRET_KEYS = frozenset(
    {
        "access_key",
        "secret_key",
        "image_store_s3_access_key",
        "image_store_s3_secret_key",
        "authorization",
        "x-amz-security-token",
    }
)


def image_store_backend_from_env(raw: str | None = None) -> str:
    text = (raw if raw is not None else os.getenv("IMAGE_STORE", DEFAULT_BACKEND)).strip().lower()
    if text in {"s3", "minio", "aws"}:
        return S3_BACKEND
    return DEFAULT_BACKEND


def default_local_root(env: dict[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    explicit = (source.get("IMAGE_STORE_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    artifacts = (source.get("ARTIFACTS_DIR") or "artifacts").strip() or "artifacts"
    return Path(artifacts) / DEFAULT_STORE_SUBDIR


def content_addressed_key(data: bytes, *, filename: str | None = None, content_type: str | None = None) -> str:
    """内容哈希命名：`ab/abcdef…ext`（两级前缀便于目录分散）。"""
    digest = hashlib.sha256(data).hexdigest()
    ext = _guess_extension(filename=filename, content_type=content_type)
    return f"{digest[:2]}/{digest}{ext}"


def _guess_extension(*, filename: str | None, content_type: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        # 允许 .jpg / .png / .webp 等短扩展名
        if suffix.startswith(".") and 2 <= len(suffix) <= 8 and all(c.isalnum() for c in suffix[1:]):
            return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed == ".jpe":
            return ".jpg"
        if guessed:
            return guessed
    return ".bin"


def _guess_content_type(*, filename: str | None, content_type: str | None) -> str:
    if content_type and content_type.strip():
        return content_type.strip()
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return DEFAULT_CONTENT_TYPE


@dataclass(frozen=True)
class S3StoreCredentials:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = DEFAULT_S3_REGION
    prefix: str = ""
    public_base_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key and self.secret_key)


def s3_credentials_from_env(env: dict[str, str] | None = None) -> S3StoreCredentials | None:
    source = env if env is not None else os.environ
    endpoint = (source.get("IMAGE_STORE_S3_ENDPOINT") or "").strip().rstrip("/")
    bucket = (source.get("IMAGE_STORE_S3_BUCKET") or "").strip()
    access_key = (source.get("IMAGE_STORE_S3_ACCESS_KEY") or "").strip()
    secret_key = (source.get("IMAGE_STORE_S3_SECRET_KEY") or "").strip()
    if not (endpoint and bucket and access_key and secret_key):
        return None
    return S3StoreCredentials(
        endpoint=endpoint,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=(source.get("IMAGE_STORE_S3_REGION") or DEFAULT_S3_REGION).strip() or DEFAULT_S3_REGION,
        prefix=(source.get("IMAGE_STORE_S3_PREFIX") or "").strip().strip("/"),
        public_base_url=(source.get("IMAGE_STORE_S3_PUBLIC_BASE_URL") or "").strip().rstrip("/"),
    )


class LocalFileImageStore(ImageStore):
    """本地文件托管：写入 artifacts 子目录，内容哈希去重。"""

    name = "local_file_image_store"

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root) if root is not None else default_local_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        data: bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> AdapterResult[StoredImage]:
        started = time.monotonic()
        if not data:
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_EMPTY", "put 需要非空图片字节。"),
                self.name,
            )
        key = content_addressed_key(data, filename=filename, content_type=content_type)
        path = self.root / key
        deduped = path.is_file() and path.stat().st_size == len(data)
        try:
            if not deduped:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        except OSError as exc:
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_STORE_IO_ERROR",
                    f"本地写盘失败：{type(exc).__name__}",
                ),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        ct = _guess_content_type(filename=filename, content_type=content_type)
        stored = StoredImage(
            key=key,
            uri=path.resolve().as_uri(),
            content_sha256=hashlib.sha256(data).hexdigest(),
            byte_size=len(data),
            content_type=ct,
            deduplicated=deduped,
            backend=DEFAULT_BACKEND,
        )
        return AdapterResult.success(stored, self.name, int((time.monotonic() - started) * 1000))

    def resolve(self, key: str) -> AdapterResult[StoredImage]:
        started = time.monotonic()
        path = self._safe_path(key)
        if path is None:
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_BAD_KEY", "非法或越界的 image store key。"),
                self.name,
            )
        if not path.is_file():
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_NOT_FOUND", f"本地对象不存在：{key}"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return AdapterResult.success(
            StoredImage(
                key=key.replace("\\", "/"),
                uri=path.resolve().as_uri(),
                content_sha256=digest,
                byte_size=path.stat().st_size,
                content_type=_guess_content_type(filename=path.name, content_type=None),
                deduplicated=True,
                backend=DEFAULT_BACKEND,
            ),
            self.name,
            int((time.monotonic() - started) * 1000),
        )

    def open_bytes(self, key: str) -> AdapterResult[bytes]:
        started = time.monotonic()
        path = self._safe_path(key)
        if path is None:
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_BAD_KEY", "非法或越界的 image store key。"),
                self.name,
            )
        if not path.is_file():
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_NOT_FOUND", f"本地对象不存在：{key}"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_STORE_IO_ERROR",
                    f"本地读盘失败：{type(exc).__name__}",
                ),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        if not data:
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_EMPTY", "本地对象字节为空。"),
                self.name,
            )
        return AdapterResult.success(data, self.name, int((time.monotonic() - started) * 1000))

    def _safe_path(self, key: str) -> Path | None:
        text = (key or "").strip().replace("\\", "/")
        if not text or text.startswith("/") or ".." in text.split("/"):
            return None
        root = self.root.resolve()
        candidate = (self.root / text).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate


def _aws_sign_v4(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    amz_date: str | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4（PutObject/GetObject 骨架；单测可注入固定 amz_date）。"""
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = quote(parsed.path or "/", safe="/~")
    canonical_query = parsed.query  # 骨架：对象 key 走 path，query 通常为空
    payload_hash = hashlib.sha256(body).hexdigest()
    now = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    datestamp = now[:8]
    hdr_map = {k.lower(): v for k, v in {**headers, "host": host}.items()}
    hdr_map["x-amz-content-sha256"] = payload_hash
    hdr_map["x-amz-date"] = now
    signed_header_names = ";".join(sorted(hdr_map))
    canonical_headers = "".join(f"{k}:{hdr_map[k].strip()}\n" for k in sorted(hdr_map))
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_header_names,
            payload_hash,
        ]
    )
    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            now,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    out = dict(hdr_map)
    out["Authorization"] = authorization
    return out


class S3CompatibleImageStore(ImageStore):
    """S3 兼容对象存储骨架（MinIO / AWS S3 等）。默认不启用；见 build_image_store。"""

    name = "s3_compatible_image_store"

    def __init__(
        self,
        creds: S3StoreCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        amz_date: str | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.creds = creds
        self._transport = transport
        self._timeout = timeout
        self._amz_date = amz_date
        self._sleep = sleep_fn

    def put(
        self,
        data: bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> AdapterResult[StoredImage]:
        started = time.monotonic()
        if not self.creds.configured:
            return AdapterResult.failure(
                AdapterError.validation(
                    "IMG_STORE_MISSING_CREDENTIALS",
                    "IMAGE_STORE=s3 但缺少 endpoint/bucket/access_key/secret_key。",
                ),
                self.name,
            )
        if not data:
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_EMPTY", "put 需要非空图片字节。"),
                self.name,
            )
        key = content_addressed_key(data, filename=filename, content_type=content_type)
        object_key = self._object_key(key)
        ct = _guess_content_type(filename=filename, content_type=content_type)
        url = self._object_url(object_key)
        headers = _aws_sign_v4(
            method="PUT",
            url=url,
            headers={"content-type": ct},
            body=data,
            access_key=self.creds.access_key,
            secret_key=self.creds.secret_key,
            region=self.creds.region,
            amz_date=self._amz_date,
        )
        from shopkeeper.retry import call_with_retry, retry_config_from_env

        def _put_once() -> AdapterResult[httpx.Response]:
            try:
                with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                    resp = client.put(url, content=data, headers=headers)
            except httpx.TimeoutException:
                return AdapterResult.failure(
                    AdapterError.generation("IMG_STORE_TIMEOUT", "S3 PutObject 超时。"),
                    self.name,
                )
            except httpx.HTTPError as exc:
                return AdapterResult.failure(
                    AdapterError.generation(
                        "IMG_STORE_HTTP_ERROR",
                        f"S3 PutObject 传输失败：{type(exc).__name__}",
                    ),
                    self.name,
                )
            except Exception as exc:  # noqa: BLE001
                return AdapterResult.failure(
                    AdapterError.generation(
                        "IMG_STORE_HTTP_ERROR",
                        f"S3 PutObject 未预期异常：{type(exc).__name__}",
                    ),
                    self.name,
                )
            if resp.status_code >= 500:
                return AdapterResult.failure(
                    AdapterError.generation(
                        "IMG_STORE_HTTP_ERROR",
                        f"S3 PutObject HTTP {resp.status_code}",
                    ),
                    self.name,
                )
            return AdapterResult.success(resp, self.name)

        put_res, _attempts = call_with_retry(
            _put_once,
            config=retry_config_from_env(),
            sleep_fn=self._sleep,
        )
        if not put_res.ok or put_res.data is None:
            err = put_res.error or AdapterError.generation(
                "IMG_STORE_HTTP_ERROR", "S3 PutObject 失败"
            )
            return AdapterResult.failure(
                err, self.name, int((time.monotonic() - started) * 1000)
            )
        resp = put_res.data
        ms = int((time.monotonic() - started) * 1000)
        if resp.status_code in {200, 201}:
            return AdapterResult.success(
                StoredImage(
                    key=key,
                    uri=self._public_uri(object_key),
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    byte_size=len(data),
                    content_type=ct,
                    deduplicated=False,
                    backend=S3_BACKEND,
                ),
                self.name,
                ms,
            )
        if resp.status_code == 404:
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_NOT_FOUND", "S3 bucket 或路径不存在。"),
                self.name,
                ms,
            )
        return AdapterResult.failure(
            AdapterError.generation(
                "IMG_STORE_HTTP_ERROR",
                f"S3 PutObject HTTP {resp.status_code}",
                upstream=_redact({"status": resp.status_code, "body": resp.text[:200]}),
            ),
            self.name,
            ms,
        )

    def resolve(self, key: str) -> AdapterResult[StoredImage]:
        """骨架：HEAD 对象确认存在；不下载全文。"""
        started = time.monotonic()
        if not self.creds.configured:
            return AdapterResult.failure(
                AdapterError.validation(
                    "IMG_STORE_MISSING_CREDENTIALS",
                    "IMAGE_STORE=s3 但缺少 endpoint/bucket/access_key/secret_key。",
                ),
                self.name,
            )
        text = (key or "").strip().replace("\\", "/")
        if not text or ".." in text.split("/"):
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_BAD_KEY", "非法或越界的 image store key。"),
                self.name,
            )
        object_key = self._object_key(text)
        url = self._object_url(object_key)
        headers = _aws_sign_v4(
            method="HEAD",
            url=url,
            headers={},
            body=b"",
            access_key=self.creds.access_key,
            secret_key=self.creds.secret_key,
            region=self.creds.region,
            amz_date=self._amz_date,
        )
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                resp = client.request("HEAD", url, headers=headers)
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_TIMEOUT", "S3 HEAD 超时。"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_STORE_HTTP_ERROR",
                    f"S3 HEAD 传输失败：{type(exc).__name__}",
                ),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        ms = int((time.monotonic() - started) * 1000)
        if resp.status_code == 404:
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_NOT_FOUND", f"S3 对象不存在：{text}"),
                self.name,
                ms,
            )
        if resp.status_code != 200:
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_STORE_HTTP_ERROR",
                    f"S3 HEAD HTTP {resp.status_code}",
                ),
                self.name,
                ms,
            )
        size = int(resp.headers.get("content-length") or 0)
        ct = resp.headers.get("content-type")
        # 内容哈希未知于 HEAD；key 内嵌 sha256 时回填，否则空串占位不编造
        digest = _digest_from_key(text) or ""
        return AdapterResult.success(
            StoredImage(
                key=text,
                uri=self._public_uri(object_key),
                content_sha256=digest,
                byte_size=size,
                content_type=ct,
                deduplicated=True,
                backend=S3_BACKEND,
            ),
            self.name,
            ms,
        )

    def open_bytes(self, key: str) -> AdapterResult[bytes]:
        started = time.monotonic()
        if not self.creds.configured:
            return AdapterResult.failure(
                AdapterError.validation(
                    "IMG_STORE_MISSING_CREDENTIALS",
                    "IMAGE_STORE=s3 但缺少 endpoint/bucket/access_key/secret_key。",
                ),
                self.name,
            )
        text = (key or "").strip().replace("\\", "/")
        if not text or ".." in text.split("/"):
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_BAD_KEY", "非法或越界的 image store key。"),
                self.name,
            )
        object_key = self._object_key(text)
        url = self._object_url(object_key)
        headers = _aws_sign_v4(
            method="GET",
            url=url,
            headers={},
            body=b"",
            access_key=self.creds.access_key,
            secret_key=self.creds.secret_key,
            region=self.creds.region,
            amz_date=self._amz_date,
        )
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_TIMEOUT", "S3 GetObject 超时。"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_STORE_HTTP_ERROR",
                    f"S3 GetObject 传输失败：{type(exc).__name__}",
                ),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        ms = int((time.monotonic() - started) * 1000)
        if resp.status_code == 404:
            return AdapterResult.failure(
                AdapterError.generation("IMG_STORE_NOT_FOUND", f"S3 对象不存在：{text}"),
                self.name,
                ms,
            )
        if resp.status_code != 200:
            return AdapterResult.failure(
                AdapterError.generation(
                    "IMG_STORE_HTTP_ERROR",
                    f"S3 GetObject HTTP {resp.status_code}",
                ),
                self.name,
                ms,
            )
        if not resp.content:
            return AdapterResult.failure(
                AdapterError.validation("IMG_STORE_EMPTY", "S3 对象字节为空。"),
                self.name,
                ms,
            )
        return AdapterResult.success(resp.content, self.name, ms)

    def _object_key(self, key: str) -> str:
        if self.creds.prefix:
            return f"{self.creds.prefix}/{key}"
        return key

    def _object_url(self, object_key: str) -> str:
        # path-style：{endpoint}/{bucket}/{key}
        encoded = "/".join(quote(part, safe="") for part in object_key.split("/"))
        return f"{self.creds.endpoint}/{self.creds.bucket}/{encoded}"

    def _public_uri(self, object_key: str) -> str:
        if self.creds.public_base_url:
            encoded = "/".join(quote(part, safe="") for part in object_key.split("/"))
            return f"{self.creds.public_base_url}/{encoded}"
        return self._object_url(object_key)


def _digest_from_key(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
        return stem
    return None


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k.lower() in _SECRET_KEYS:
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def build_image_store(
    *,
    backend: str | None = None,
    env: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    local_root: str | os.PathLike[str] | None = None,
) -> ImageStore:
    """按环境装配托管后端：默认本地；仅 s3+凭据齐全才启用 S3 骨架。"""
    chosen = image_store_backend_from_env(
        None if env is None else env.get("IMAGE_STORE")
    ) if backend is None else (
        S3_BACKEND if backend.strip().lower() in {"s3", "minio", "aws"} else DEFAULT_BACKEND
    )
    if chosen == S3_BACKEND:
        creds = s3_credentials_from_env(env)
        if creds is not None and creds.configured:
            return S3CompatibleImageStore(creds, transport=transport)
        logger.info("IMAGE_STORE=s3 但凭据不全，回退 LocalFileImageStore")
    root = local_root if local_root is not None else default_local_root(env)
    return LocalFileImageStore(root)
