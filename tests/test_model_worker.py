"""ModelWorker 的离线测试：QObject 直接构造 + 同步调用 run()（不经 QThread/事件循环），
signal.emit() 在没有 QApplication 的情况下也能同步触发已连接的 Python 回调，不需要
搭建离屏 Qt 环境。
"""
import threading

import pytest

from comic_gen.ui.workers import ModelWorker


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch, tmp_path):
    import comic_gen.core.environment as env_mod

    monkeypatch.setattr(env_mod, "apply_model_dir", lambda model_dir: None)


class _FakeSettings:
    model_dir = ""
    model_files = {}
    device = "cuda"


def test_precancelled_worker_skips_every_model_without_attempting_download(monkeypatch):
    """取消是协作式的：只在"开始下一个模型之前"检查。这里模拟用户在下载开始前就已
    经点了停止（cancel_event 提前 set），预期一个模型都不会尝试下载/检查缓存。
    """
    import comic_gen.core.environment as env_mod

    called = []
    monkeypatch.setattr(env_mod, "is_model_cached", lambda *a, **k: called.append(1) or True)

    cancel_event = threading.Event()
    cancel_event.set()
    logs = []
    finished = []
    w = ModelWorker(_FakeSettings(), ["detect", "lama"], True, cancel_event)
    w.log.connect(logs.append)
    w.finished.connect(finished.append)
    w.run()

    assert called == []  # 一次 is_model_cached 都没调用过
    assert "已停止" in finished[0]
    assert "剩余 2" in finished[0] or "2 个" in finished[0]


def test_cancel_mid_batch_lets_current_model_finish_then_stops_before_next(monkeypatch):
    """取消在处理第一个模型期间被请求：第一个模型的结果仍然完整落进 finished 摘要，
    第二个模型完全不碰。"""
    import comic_gen.core.environment as env_mod

    cancel_event = threading.Event()
    call_count = 0

    def fake_is_model_cached(key, model_dir=None, bindings=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            cancel_event.set()  # 模拟用户在第一个模型检查/下载期间点了停止
        return True

    monkeypatch.setattr(env_mod, "is_model_cached", fake_is_model_cached)

    logs = []
    finished = []
    w = ModelWorker(_FakeSettings(), ["detect", "lama"], True, cancel_event)
    w.log.connect(logs.append)
    w.finished.connect(finished.append)
    w.run()

    assert call_count == 1  # 第二个模型完全没碰 is_model_cached
    summary = finished[0]
    assert "检测模型" in summary  # 第一个模型的结果完整保留
    assert "已停止" in summary


def test_download_progress_streams_per_model_index(monkeypatch):
    """3.3：之前 ModelWorker.log 信号存在但 UI 只连了 finished，下载期间用户看不到
    任何反馈。这里验证 log 信号本身确实按 (i/total) 逐个模型发出，UI 端只需要接上
    show_status 就有真实的流式反馈——不是新造一套进度机制。

    is_model_cached 返回 False 会真的走到 build(key)——那是个跑真实模型构造/下载
    的闭包，必须把 detect/lama 用到的具体类换成不碰网络/磁盘的假实现，否则这条测试
    会尝试真的下载模型。
    """
    import comic_gen.core.environment as env_mod
    import comic_gen.pipeline.detect as detect_mod
    import comic_gen.pipeline.inpaint as inpaint_mod

    monkeypatch.setattr(env_mod, "is_model_cached", lambda *a, **k: False)
    monkeypatch.setattr(detect_mod, "ComicDetector", lambda *a, **k: None)
    monkeypatch.setattr(inpaint_mod, "Inpainter", lambda *a, **k: None)

    logs = []
    w = ModelWorker(_FakeSettings(), ["detect", "lama"], True, None)
    w.log.connect(logs.append)
    finished = []
    w.finished.connect(finished.append)
    w.run()

    assert any("(1/2)" in m for m in logs)
    assert any("(2/2)" in m for m in logs)
    assert "[OK]" in finished[0]


def test_unexpected_exception_reports_cleanly_without_crashing_on_missing_import(monkeypatch):
    """回归测试：之前这个文件用了 traceback.format_exc() 却从没 import traceback，
    outer except 块本身就会因为 NameError 崩溃，把真实异常信息替换成一个更难懂的
    "name 'traceback' is not defined"。这里让 apply_model_dir 抛异常触发 outer except，
    验证 finished 拿到的是清楚的失败信息，而不是二次崩溃。
    """
    import comic_gen.core.environment as env_mod

    monkeypatch.setattr(env_mod, "apply_model_dir", lambda model_dir: (_ for _ in ()).throw(RuntimeError("boom")))

    finished = []
    w = ModelWorker(_FakeSettings(), ["detect"], True, None)
    w.finished.connect(finished.append)
    w.run()

    assert finished, "finished 信号必须被发出，不能让 outer except 自己再崩一次"
    assert "失败" in finished[0]
    assert "boom" in finished[0]
    assert "NameError" not in finished[0]


def test_lama_build_uses_settings_device_not_hardcoded_cuda(monkeypatch):
    """LaMa 的"检查/下载"闭包用 settings.device 构造 Inpainter——硬编码 "cuda" 会让
    CPU 机器把"已下载成功"误报成失败（构造即加载，设备错会抛）。"""
    import comic_gen.core.environment as env_mod
    import comic_gen.pipeline.inpaint as inpaint_mod

    monkeypatch.setattr(env_mod, "is_model_cached", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(inpaint_mod, "Inpainter", lambda *a, **k: calls.append(a) or None)

    class _CpuSettings:
        model_dir = ""
        model_files = {}
        device = "cpu"

    finished = []
    w = ModelWorker(_CpuSettings(), ["lama"], True, None)
    w.finished.connect(finished.append)
    w.run()

    assert calls == [("cpu",)]  # 位置参数 device 来自设置，不再写死 "cuda"
    assert "[OK]" in finished[0]
