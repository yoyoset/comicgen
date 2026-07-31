"""core.errors 的异常层次测试。"""
import pytest

from comic_gen.core.errors import ComicGenError, InputError, ModelError, OutputError, Stage


def test_comic_gen_error_default_technical_equals_user_message():
    e = ComicGenError("人话")
    assert e.user_message == "人话"
    assert e.technical == "人话"
    assert str(e) == "人话"


def test_comic_gen_error_separate_technical():
    e = ComicGenError("人话", technical="Traceback: ...")
    assert e.user_message == "人话"
    assert e.technical == "Traceback: ..."


def test_subclasses_are_comic_gen_error():
    assert issubclass(ModelError, ComicGenError)
    assert issubclass(InputError, ComicGenError)
    assert issubclass(OutputError, ComicGenError)


def test_subclasses_catchable_as_base():
    with pytest.raises(ComicGenError):
        raise ModelError("模型缺失")
    with pytest.raises(ComicGenError):
        raise InputError("输入不合法")
    with pytest.raises(ComicGenError):
        raise OutputError("输出不安全")


def test_stage_values():
    assert Stage.DETECT == "detect"
    assert Stage.OCR == "ocr"
    assert Stage.TRANSLATE == "translate"
    assert Stage.RENDER == "render"


def test_stage_is_str_for_json_serialization():
    import json

    assert json.dumps(Stage.DETECT) == '"detect"'
