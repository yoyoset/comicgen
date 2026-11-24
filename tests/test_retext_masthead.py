"""retext_volume 的页眉页脚守卫（不重排版式字）回归测试。"""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retext_volume.py"


def _load():
    spec = importlib.util.spec_from_file_location("retext_volume", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rv = _load()


def test_masthead_detected():
    assert rv._is_masthead("弗力札篇——2 ★36")
    assert rv._is_masthead("弗力札篇—2★166")
    assert rv._is_masthead("死神 18")
    assert rv._is_masthead("117 ★ DRAGON BALL FULL COLOR")


def test_dialogue_not_mistaken_for_masthead():
    assert not rv._is_masthead("死神。")
    assert not rv._is_masthead("我是最强的…")
    assert not rv._is_masthead(
        "比起其他那美克星人，他给我的感觉格外厉害，而且看起来很强"
    )
