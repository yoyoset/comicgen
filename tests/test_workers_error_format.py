"""ui.workers.format_job_failure 的纯函数测试（不需要 Qt/QThread）。"""
from comic_gen.core.errors import InputError, ModelError
from comic_gen.ui.workers import format_job_failure


def test_comic_gen_error_shows_only_user_message():
    e = ModelError("设置里绑定的模型文件已找不到，请到「模型管理」页重新定位。",
                    technical="FileNotFoundError: F:/models/foo.gguf")
    result = format_job_failure(e)
    assert result == "失败：设置里绑定的模型文件已找不到，请到「模型管理」页重新定位。"
    assert "FileNotFoundError" not in result
    assert "Traceback" not in result


def test_input_error_also_uses_user_message():
    e = InputError("不支持的输入类型")
    assert format_job_failure(e) == "失败：不支持的输入类型"


def test_unexpected_exception_includes_full_traceback():
    """未预期的异常（不是 ComicGenError）仍然要带完整 traceback，不能藏起来。"""
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        result = format_job_failure(e)
    assert "失败：boom" in result
    assert "Traceback" in result
    assert "RuntimeError" in result


def test_oserror_gets_friendly_message_without_traceback():
    """OSError（路径无效/无写权限/磁盘满）是用户最常撞的失败，不该把整段 Python 堆栈
    糊给用户——OSError 消息本身带失败路径和 errno，够定位。完整堆栈仍由调用方落 run.log。"""
    e = PermissionError(13, "拒绝访问", r"D:\readonly\pages.json")
    result = format_job_failure(e)
    assert "失败" in result
    assert "文件操作失败" in result
    assert "Traceback" not in result  # 不糊堆栈
    assert "pages.json" in result  # 但保留失败路径


def test_oserror_file_not_found_is_friendly():
    try:
        raise FileNotFoundError(2, "找不到文件", "missing.png")
    except OSError as e:
        result = format_job_failure(e)
    assert "Traceback" not in result
    assert "missing.png" in result
