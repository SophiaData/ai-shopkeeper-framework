"""结构化错误：适配器失败必须返回错误码，不裸抛异常（DESIGN §3.3、§9 M1-B）。

错误码分两类，处置路径不同（DESIGN §3.3）：
- 生成类（GENERATION）：可重试 2 次，仍失败 → 降级人工待办
- 执行类（EXECUTION）：立即停、保留现场、进人工待办，不自动重试
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ErrorKind(StrEnum):
    """错误的处置语义，决定编排器是重试还是停下。"""

    GENERATION = "generation"  # 重试 2 次后降级人工待办
    EXECUTION = "execution"  # 立即停，不自动重试（防重复下单）
    VALIDATION = "validation"  # 输入本身不合法，重试无意义
    POLICY = "policy"  # 合规/闸门拒绝，永不自动放行


class AdapterError(BaseModel):
    """所有适配器返回的错误载荷。code 必须可定位（C3 错误码表同源）。"""

    code: str
    kind: ErrorKind
    message: str
    retryable: bool
    detail: dict | None = None
    human_todo: bool = False

    @classmethod
    def generation(cls, code: str, message: str, **kw) -> "AdapterError":
        return cls(code=code, kind=ErrorKind.GENERATION, message=message, retryable=True, detail=kw or None)

    @classmethod
    def execution(cls, code: str, message: str, **kw) -> "AdapterError":
        return cls(code=code, kind=ErrorKind.EXECUTION, message=message, retryable=False, human_todo=True, detail=kw or None)

    @classmethod
    def validation(cls, code: str, message: str, **kw) -> "AdapterError":
        return cls(code=code, kind=ErrorKind.VALIDATION, message=message, retryable=False, detail=kw or None)

    @classmethod
    def policy(cls, code: str, message: str, **kw) -> "AdapterError":
        return cls(code=code, kind=ErrorKind.POLICY, message=message, retryable=False, human_todo=True, detail=kw or None)
