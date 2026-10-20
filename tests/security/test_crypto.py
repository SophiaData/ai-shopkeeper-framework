"""C3-1b：密文信封（SCHEMA §4.4 第 4 条字节规范）。

断言的是**字节布局本身**，不只是"能加密能解密"——
规范写死了 version(1)||key_id(2)||nonce(12)||ciphertext||tag(16)，
布局错了以后跨实现就对不上。
"""

from __future__ import annotations

import pytest

from shopkeeper.security.crypto import (
    HEADER_LEN,
    NONCE_LEN,
    TAG_LEN,
    VERSION,
    CryptoError,
    KeyRing,
    TamperedError,
    UnknownKeyError,
    aad_for,
    make_key,
)

ROW = "orders:o_1"
FIELD = "address"


@pytest.fixture()
def ring() -> KeyRing:
    return KeyRing.generate(key_id=1)


def test_envelope_byte_layout_matches_spec(ring):
    sealed = ring.seal("科技园南路12号", row_key=ROW, field_name=FIELD)
    blob = sealed.blob
    assert blob[0] == VERSION
    assert int.from_bytes(blob[1:3], "big") == 1
    assert len(blob) == HEADER_LEN + len("科技园南路12号".encode()) + TAG_LEN
    assert ring.key_id_of(blob) == 1


def test_roundtrip(ring):
    secret = "13812345678"
    sealed = ring.seal(secret, row_key=ROW, field_name=FIELD)
    assert secret not in sealed.blob.decode("latin-1")
    assert ring.open(sealed.blob, row_key=ROW, field_name=FIELD).decode() == secret


def test_nonce_is_random_so_same_plaintext_differs_each_time(ring):
    a = ring.seal("x", row_key=ROW, field_name=FIELD).blob
    b = ring.seal("x", row_key=ROW, field_name=FIELD).blob
    assert a != b, "nonce 复用会削弱 GCM 安全性"


def test_aad_binds_row_and_field(ring):
    sealed = ring.seal("addr-secret", row_key=ROW, field_name=FIELD)
    with pytest.raises(TamperedError):
        ring.open(sealed.blob, row_key="orders:o_2", field_name=FIELD)
    with pytest.raises(TamperedError):
        ring.open(sealed.blob, row_key=ROW, field_name="phone")


def test_aad_string_shape_is_the_spec_concatenation():
    """§4.4 的 `||` 是拼接记法，结果必须是 row|field。"""
    assert aad_for("orders:o1", "phone") == b"orders:o1|phone"


def test_tampered_ciphertext_rejected(ring):
    sealed = bytearray(ring.seal("secret", row_key=ROW, field_name=FIELD).blob)
    sealed[HEADER_LEN] ^= 0xFF
    with pytest.raises(TamperedError):
        ring.open(bytes(sealed), row_key=ROW, field_name=FIELD)


def test_truncated_blob_rejected(ring):
    with pytest.raises(CryptoError):
        ring.open(b"\x01\x00\x01short", row_key=ROW, field_name=FIELD)


def test_unknown_version_rejected(ring):
    sealed = ring.seal("x", row_key=ROW, field_name=FIELD)
    bad = bytes([9]) + sealed.blob[1:]
    with pytest.raises(TamperedError):
        ring.open(bad, row_key=ROW, field_name=FIELD)


# ---------------------------------------------------------------- 轮换（双密钥读期）

def test_rotation_keeps_old_blobs_readable_then_retires():
    """§4.4 轮换三步：加新读钥 → 切写钥 → 重写完后撤旧钥。"""
    old = KeyRing({1: make_key()}, write_key_id=1)
    legacy = old.seal("历史地址", row_key=ROW, field_name=FIELD)

    new_key = make_key()
    old.add_read_key(2, new_key)
    old.promote_write(2)
    assert old.write_key_id == 2

    fresh = old.seal("新地址", row_key=ROW, field_name=FIELD)
    assert fresh.key_id == 2
    assert legacy.key_id == 1

    # 双密钥读期：新旧都能读
    assert old.open(legacy.blob, row_key=ROW, field_name=FIELD).decode() == "历史地址"
    assert old.open(fresh.blob, row_key=ROW, field_name=FIELD).decode() == "新地址"

    # 撤旧钥后：新数据照常，历史数据明确报错（而不是静默返回垃圾）
    old.retire_read_key(1)
    assert old.open(fresh.blob, row_key=ROW, field_name=FIELD).decode() == "新地址"
    with pytest.raises(CryptoError):
        old.open(legacy.blob, row_key=ROW, field_name=FIELD)


def test_cannot_retire_current_write_key(ring):
    with pytest.raises(CryptoError):
        ring.retire_read_key(ring.write_key_id)


def test_promote_requires_registered_key(ring):
    with pytest.raises(UnknownKeyError):
        ring.promote_write(99)


def test_key_length_enforced():
    with pytest.raises(ValueError):
        KeyRing({1: b"short"}, 1)
    with pytest.raises(ValueError):
        KeyRing({1: make_key()}, 2)  # write id 不在册


def test_nonce_and_tag_lengths_follow_spec():
    assert (NONCE_LEN, TAG_LEN) == (12, 16)
    assert HEADER_LEN == 1 + 2 + 12
