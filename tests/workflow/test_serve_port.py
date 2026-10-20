"""serve 端口占用时给出中文提示（Windows Errno 10048 / Linux EADDRINUSE）。"""

from __future__ import annotations

import socket

import pytest

from shopkeeper.workflow.serve import (
    ensure_port_available,
    is_addr_in_use_error,
    port_in_use_message,
)


def test_port_in_use_message_mentions_workflow_port():
    text = port_in_use_message("127.0.0.1", 8000)
    assert "WORKFLOW_PORT" in text
    assert "8000" in text
    assert "python -m shopkeeper.workflow.serve" in text


def test_is_addr_in_use_recognizes_windows_and_linux_codes():
    assert is_addr_in_use_error(OSError(98, "Address already in use"))
    assert is_addr_in_use_error(OSError(10048, "Only one usage of each socket address"))
    win = OSError(22, "ignored")
    win.winerror = 10048  # type: ignore[attr-defined]
    assert is_addr_in_use_error(win)
    assert not is_addr_in_use_error(OSError(13, "Permission denied"))
    assert not is_addr_in_use_error(ValueError("nope"))


def test_ensure_port_available_exits_with_chinese_hint(capsys):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    host, port = holder.getsockname()[:2]
    try:
        with pytest.raises(SystemExit) as exited:
            ensure_port_available(host, port)
        assert exited.value.code == 1
        err = capsys.readouterr().err
        assert "端口已被占用" in err
        assert "WORKFLOW_PORT" in err
    finally:
        holder.close()
