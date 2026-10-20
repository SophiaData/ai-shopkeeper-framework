"""日志 / 告警出站前的 PII 脱敏（泰国站 PDPA 配套）。

落库守卫见 ``redaction.guard_payload``（SCHEMA §4.4）。
本模块只负责**输出路径**：Formatter 与告警体在写出前跑同一套片段规则，
避免手机号/地址/邮箱进 stderr、webhook 或飞书文本。

**非法律意见**：保留与脱敏是工程默认；合规义务以适用法律与专业意见为准。
"""

from __future__ import annotations

import logging
from typing import Any

from shopkeeper.security.redaction import REDACTED, guard_payload, redact_spans


def redact_log_text(text: str) -> str:
    """对自由文本做片段脱敏；空串原样返回。"""
    if not text:
        return text
    scrubbed, _hit = redact_spans(text)
    return scrubbed


def redact_log_value(value: Any) -> Any:
    """递归脱敏：dict/list 走字段名+内容嗅探；str 走片段替换。"""
    if isinstance(value, str):
        return redact_log_text(value)
    if isinstance(value, dict):
        cleaned, _rep = guard_payload(value, sniff=True)
        return cleaned
    if isinstance(value, (list, tuple)):
        cleaned, _rep = guard_payload(list(value), sniff=True)
        return cleaned
    return value


class PiiRedactingFilter(logging.Filter):
    """在 Formatter 之前改写 LogRecord 的 msg/args，使 getMessage() 已脱敏。

    不改 exc_info 对象本身；异常文本在 Formatter 里再 scrub 一次。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_log_text(record.msg)
            elif record.msg is not None:
                record.msg = redact_log_value(record.msg)

            args = record.args
            if args is None:
                return True
            if isinstance(args, dict):
                # 必须「整份 dict 交给守卫」，不能逐 value 脱敏：字段名带语义
                # （sign / access_token 是凭据，只看值看不出来）。逐 value 映射会
                # 把 key 丢掉，dict 形态的 %(name)s 参数永远漏网。
                # 实测：旧写法下 `info("sign=%(sign)s", {"sign": "deadbeef"})` 原样输出。
                record.args = redact_log_value(dict(args))
            elif isinstance(args, tuple):
                record.args = tuple(redact_log_value(a) for a in args)
            else:
                record.args = redact_log_value(args)
        except Exception:  # noqa: BLE001 — 脱敏失败不得阻断日志
            record.msg = REDACTED
            record.args = ()
        return True
