"""app.batch_queue 的离线测试：队列核心逻辑（增删/去重/顺序/重跑/清空），纯函数不碰 Qt。"""
from comic_gen.app.batch_queue import (
    QUEUE_STATUS_CANCELLED,
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_OK,
    QUEUE_STATUS_PARTIAL,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_RUNNING,
    BatchJob,
    BatchQueue,
)


def test_add_returns_none_and_appends():
    q = BatchQueue()
    assert q.add("a.zip") is None
    assert q.add("b/folder") is None
    assert len(q.jobs()) == 2
    assert q.jobs()[0].status == QUEUE_STATUS_PENDING


def test_add_duplicate_is_rejected_with_human_message(tmp_path):
    """同一个输入（按解析后绝对路径）加两次要被拦下并说明原因，不能静默重复。"""
    q = BatchQueue()
    p = tmp_path / "book.zip"
    p.touch()
    assert q.add(str(p)) is None
    err = q.add(str(p))
    assert err is not None and "已在队列中" in err
    # 路径写法不同（相对/绝对、分隔符）也算重复
    assert q.add(str(p.resolve())) is not None
    assert len(q.jobs()) == 1


def test_next_pending_returns_in_order():
    q = BatchQueue()
    q.add("a.zip")
    q.add("b.zip")
    q.jobs()[0].status = QUEUE_STATUS_OK  # 第一个已完成
    nxt = q.next_pending()
    assert nxt is q.jobs()[1]
    assert nxt.input_path == "b.zip"


def test_next_pending_none_when_all_done():
    q = BatchQueue()
    q.add("a.zip")
    q.jobs()[0].status = QUEUE_STATUS_OK
    assert q.next_pending() is None


def test_remove_running_job_is_rejected():
    q = BatchQueue()
    q.add("a.zip")
    job = q.jobs()[0]
    job.status = QUEUE_STATUS_RUNNING
    assert "正在运行" in q.remove(job)
    assert len(q.jobs()) == 1


def test_remove_pending_job_succeeds():
    q = BatchQueue()
    q.add("a.zip")
    q.add("b.zip")
    job = q.jobs()[0]
    assert q.remove(job) is None
    assert [j.input_path for j in q.jobs()] == ["b.zip"]


def test_reset_for_rerun_only_affects_rerunnable():
    q = BatchQueue()
    q.add("a.zip")          # ok —— 保持不动
    q.add("b.zip")          # failed —— 打回
    q.add("c.zip")          # cancelled —— 打回
    q.add("d.zip")          # partial —— 打回
    q.add("e.zip")          # pending —— 不动
    jobs = q.jobs()
    jobs[0].status, jobs[1].status = QUEUE_STATUS_OK, QUEUE_STATUS_FAILED
    jobs[2].status, jobs[3].status = QUEUE_STATUS_CANCELLED, QUEUE_STATUS_PARTIAL
    jobs[1].summary = "x"

    n = q.reset_for_rerun()
    assert n == 3
    assert jobs[0].status == QUEUE_STATUS_OK
    assert jobs[1].status == QUEUE_STATUS_PENDING and jobs[1].summary == ""
    assert jobs[2].status == QUEUE_STATUS_PENDING
    assert jobs[3].status == QUEUE_STATUS_PENDING
    assert jobs[4].status == QUEUE_STATUS_PENDING


def test_clear_finished_keeps_pending_and_running():
    q = BatchQueue()
    q.add("a.zip"); q.add("b.zip"); q.add("c.zip")
    jobs = q.jobs()
    jobs[0].status = QUEUE_STATUS_OK
    jobs[1].status = QUEUE_STATUS_PENDING
    jobs[2].status = QUEUE_STATUS_RUNNING
    removed = q.clear_finished()
    assert removed == 1
    assert [j.input_path for j in q.jobs()] == ["b.zip", "c.zip"]


def test_counts_summary():
    q = BatchQueue()
    q.add("a.zip"); q.add("b.zip"); q.add("c.zip"); q.add("d.zip")
    jobs = q.jobs()
    jobs[0].status = QUEUE_STATUS_OK
    jobs[1].status = QUEUE_STATUS_PARTIAL
    jobs[2].status = QUEUE_STATUS_FAILED
    c = q.counts()
    assert c["total"] == 4 and c["done"] == 2 and c["failed"] == 1 and c["pending"] == 1


def test_has_work_true_only_for_pending_or_rerunnable():
    q = BatchQueue()
    q.add("a.zip")  # pending
    assert q.has_work() is True
    q.jobs()[0].status = QUEUE_STATUS_OK
    assert q.has_work() is False
    q.jobs()[0].status = QUEUE_STATUS_FAILED
    assert q.has_work() is True
    q.jobs()[0].status = QUEUE_STATUS_CANCELLED
    assert q.has_work() is True
    q.jobs()[0].status = QUEUE_STATUS_PARTIAL
    assert q.has_work() is True
    q.jobs()[0].status = QUEUE_STATUS_RUNNING
    assert q.has_work() is False


def test_display_output_falls_back():
    assert BatchJob(input_path="a.zip").display_output == "（默认推导）"
    assert BatchJob(input_path="a.zip", output_path="out").display_output == "out"
    assert BatchJob(input_path="a.zip", resolved_output="out2").display_output == "out2"
