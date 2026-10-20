"""加密 blob 信封（SCHEMA §4.4 第 4 条的字节规范）。

    version(1) || key_id(2) || nonce(12) || ciphertext || tag(16)

AAD = `{row_key}||'|'||{field_name}` —— 与 HMAC 指纹同构的域分离：
同一明文加密进不同行/不同字段，密文也不同，拿密文做跨行比对推不出"是同一个手机号"。

轮换：`KeyRing` 支持多把读钥（双密钥读期）+ 一把写钥；
解不开时逐把试读钥，全失败才报错——这样换钥期间旧 blob 仍可读。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1
NONCE_LEN = 12
TAG_LEN = 16
HEADER_LEN = 1 + 2 + NONCE_LEN  # version + key_id + nonce


class CryptoError(Exception):
    pass


class UnknownKeyError(CryptoError):
    pass


class TamperedError(CryptoError):
    """AAD 不匹配 / 密文被改 / 版本不认识。"""


def make_key() -> bytes:
    return secrets.token_bytes(32)


def aad_for(row_key: str, field_name: str) -> bytes:
    """AAD 绑定行与字段。§4.4 写作 `{row_key}||'|'||{field_name}`——
    那是**拼接记法**（`||` = 串接、`'|'` = 字面竖线），不是把 `||'||` 当字符塞进去。
    结果就是 `row_key|field_name`，与 `redaction.hmac_fingerprint` 同一种串法，
    两边必须一致，否则"同一行同字段"的指纹与密文互相对不上。
    """
    return f"{row_key}|{field_name}".encode("utf-8")


@dataclass
class Sealed:
    blob: bytes
    key_id: int
    version: int = VERSION

    def hex(self) -> str:
        return self.blob.hex()


class KeyRing:
    """多读一写。写用 `write_key_id`，读优先写钥、失败再试其它在册钥（双密钥读期）。"""

    def __init__(self, keys: dict[int, bytes], write_key_id: int) -> None:
        if write_key_id not in keys:
            raise ValueError("write_key_id 必须在 keys 里")
        for kid, k in keys.items():
            if len(k) != 32:
                raise ValueError(f"key_id={kid} 长度 {len(k)}，AES-256 需要 32 字节")
        self._keys = dict(keys)
        self._write_id = int(write_key_id)

    @classmethod
    def generate(cls, key_id: int = 1) -> "KeyRing":
        return cls({key_id: make_key()}, key_id)

    @property
    def write_key_id(self) -> int:
        return self._write_id

    def add_read_key(self, key_id: int, key: bytes) -> None:
        """轮换第 1 步：先把新钥加进读集。"""
        if len(key) != 32:
            raise ValueError("AES-256 需要 32 字节密钥")
        self._keys[int(key_id)] = key

    def promote_write(self, key_id: int) -> None:
        """轮换第 2 步：新写的改用新钥（旧读钥保留，进入双密钥读期）。"""
        key_id = int(key_id)
        if key_id not in self._keys:
            raise UnknownKeyError(f"key_id={key_id} 未注册，先 add_read_key")
        self._write_id = key_id

    def retire_read_key(self, key_id: int) -> None:
        """轮换第 3 步：确认历史 blob 已重写完毕后才撤旧钥。"""
        key_id = int(key_id)
        if key_id == self._write_id:
            raise CryptoError("不能撤掉当前写钥")
        self._keys.pop(key_id, None)

    # ------------------------------------------------------------------

    def key_id_of(self, blob: bytes) -> int:
        """读信封头，不解密就能知道是哪把钥（运维排查轮换问题要用）。"""
        if len(blob) < HEADER_LEN:
            raise CryptoError("blob 太短，读不出 key_id")
        return int.from_bytes(blob[1:3], "big")

    def seal(self, plaintext: str | bytes, *, row_key: str, field_name: str) -> Sealed:
        key = self._keys[self._write_id]
        nonce = os.urandom(NONCE_LEN)
        body_tag = AESGCM(key).encrypt(nonce, _to_bytes(plaintext), aad_for(row_key, field_name))
        # AESGCM 把 tag 拼在密文尾部，正好对上 §4.4 的 ciphertext||tag(16)
        blob = bytes([VERSION]) + self._write_id.to_bytes(2, "big") + nonce + body_tag
        return Sealed(blob=blob, key_id=self._write_id)

    def open(self, blob: bytes, *, row_key: str, field_name: str) -> bytes:
        if len(blob) < HEADER_LEN + TAG_LEN:
            raise TamperedError(f"blob 过短: {len(blob)} 字节")
        version = blob[0]
        if version != VERSION:
            raise TamperedError(f"不支持的 version={version}")
        key_id = int.from_bytes(blob[1:3], "big")
        nonce = blob[3 : 3 + NONCE_LEN]
        body_tag = blob[3 + NONCE_LEN :]
        aad = aad_for(row_key, field_name)
        # 先试写钥（常态），再试其余读钥（双密钥读期）
        order = [key_id] if key_id in self._keys else []
        order += [k for k in self._keys if k not in order]
        if not order:
            raise UnknownKeyError(f"key_id={key_id} 不在册，且无可用的候选读钥")
        last: Exception | None = None
        for candidate in order:
            try:
                return AESGCM(self._keys[candidate]).decrypt(nonce, body_tag, aad)
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise TamperedError(f"解密失败（AAD 不匹配或密文被改）: {last}")


def _to_bytes(v: str | bytes) -> bytes:
    return v.encode("utf-8") if isinstance(v, str) else v
