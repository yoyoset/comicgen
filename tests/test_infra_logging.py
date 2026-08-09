"""infra.logging 的测试：纯标准库行为，不需要 Qt/模型。

logger 是模块级单例，每个测试必须自己 detach 掉挂上去的 handler，否则会在
测试之间累积（后面的测试会收到前面测试挂的 callback 的转发）。
"""

import pytest

from comic_gen.infra.logging import (
    attach_callback_handler,
    attach_job_file_handler,
    detach_handler,
    logger,
)


@pytest.fixture(autouse=True)
def _clean_handlers():
    """防止某个测试挂了 handler 忘记 detach，污染后续测试。"""
    before = list(logger.handlers)
    yield
    for h in list(logger.handlers):
        if h not in before:
            logger.removeHandler(h)
            h.close()


def test_callback_handler_forwards_message():
    received = []
    handler = attach_callback_handler(received.append)
    try:
        logger.info("hello")
        assert len(received) == 1
        assert "hello" in received[0]
        assert "[INFO]" in received[0]
    finally:
        detach_handler(handler)


def test_detach_stops_forwarding():
    received = []
    handler = attach_callback_handler(received.append)
    detach_handler(handler)
    logger.info("should not arrive")
    assert received == []


def test_two_handlers_each_get_one_call():
    a, b = [], []
    ha = attach_callback_handler(a.append)
    hb = attach_callback_handler(b.append)
    try:
        logger.warning("dup check")
        assert len(a) == 1
        assert len(b) == 1
    finally:
        detach_handler(ha)
        detach_handler(hb)


def test_callback_exception_does_not_propagate():
    """转发失败不该反过来打断调用 logger.xxx(...) 的那行代码。"""

    def boom(msg):
        raise RuntimeError("callback broke")

    handler = attach_callback_handler(boom)
    try:
        logger.info("should not raise")  # 不应抛异常
    finally:
        detach_handler(handler)


def test_reassigning_underlying_callable_is_picked_up_dynamically():
    """callback 在每次 emit 时才调用，不是在 attach 时捕获——模拟 Runner.log
    被 UI 重新赋值后，转发目标要跟着变。
    """
    calls = {"target": "a"}
    log_a, log_b = [], []

    def dynamic_callback(msg):
        (log_a if calls["target"] == "a" else log_b).append(msg)

    handler = attach_callback_handler(dynamic_callback)
    try:
        logger.info("first")
        calls["target"] = "b"
        logger.info("second")
        assert len(log_a) == 1
        assert len(log_b) == 1
    finally:
        detach_handler(handler)


def test_job_file_handler_writes_timestamped_lines(tmp_path):
    handler = attach_job_file_handler(tmp_path)
    try:
        logger.warning("到此一游")
    finally:
        detach_handler(handler)

    content = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "到此一游" in content
    assert "[WARNING]" in content
    assert "comic_gen" in content


def test_debug_level_filtered_by_default():
    """logger 设的是 INFO，DEBUG 记录不该被转发出去。"""
    received = []
    handler = attach_callback_handler(received.append)
    try:
        logger.debug("noise")
        logger.info("signal")
        assert received == ["[INFO] signal"]
    finally:
        detach_handler(handler)
