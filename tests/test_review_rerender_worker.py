"""ReviewRerenderWorker 的离线测试：同步调用 run()，不经 QThread/事件循环
（和 test_model_worker.py 同样的理由：直接连接的 signal.emit() 不需要 QApplication）。
"""
from pathlib import Path

from comic_gen.ui.workers import ReviewRerenderWorker


class _FakeSession:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def rerender_page(self, image):
        self.calls.append(image)
        if self._error is not None:
            raise self._error
        return self._result


def test_success_emits_image_and_path():
    session = _FakeSession(result=Path("out/page_0.png"))
    w = ReviewRerenderWorker(session, "page_0.png")
    received = []
    w.finished.connect(lambda image, result: received.append((image, result)))
    w.run()

    assert received == [("page_0.png", Path("out/page_0.png"))]
    assert session.calls == ["page_0.png"]


def test_failure_emits_image_and_exception_instead_of_raising():
    session = _FakeSession(error=RuntimeError("boom"))
    w = ReviewRerenderWorker(session, "page_0.png")
    received = []
    w.finished.connect(lambda image, result: received.append((image, result)))
    w.run()  # 不应该向外抛异常——QThread 里的未捕获异常会直接终止线程且不留痕迹

    assert len(received) == 1
    image, result = received[0]
    assert image == "page_0.png"
    assert isinstance(result, RuntimeError)
