"""审计比对工具的回归测试（Qt-free、无模型、纯文本）。

锁定三条容易被"优化"掉的行为：
1. 繁体差异不算错（渲染前统一 zhconv），但**标点差异必须算**——校对时补的「。」
   是成品内容，当无差异会在下次整卷重排时丢掉。
2. 台湾用语归一表命中后要真的改字，否则「好康」这种永远进不了 fix.json。
3. 清洗步骤要把抄图带进来的空格/半角标点清掉。
"""
import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit_diff = _load("audit_diff")
clean_trans = _load("clean_trans")


def test_normalize_tolerates_whitespace_but_not_punctuation():
    assert audit_diff.normalize("好 了") == audit_diff.normalize("好了")
    assert audit_diff.normalize("好") != audit_diff.normalize("好。")


def test_normalize_tolerates_traditional():
    pytest.importorskip("zhconv")
    assert audit_diff.normalize("龍珠") == audit_diff.normalize("龙珠")


def test_twisms_apply():
    assert audit_diff.apply_twisms("上次也是你抢走最好康的…") == "上次也是你抢走最好的…"
    assert audit_diff.apply_twisms("没什么好康") == "没什么好处"


def test_blank_transcription_becomes_explicit_blank(tmp_path):
    """转录里写空字符串 = 明确要求「保留原图」。

    实测背景：拟声词/美术字不该重排，但只把转录值留空、不写进 fix.json 的话，
    retext 会沿用 text.jsonl 的旧文字把这格重排成黑体（0044 四个彩色拟声词被覆盖）。
    所以空串必须落成 fix 里的 ""。
    """
    import json as _json

    series = tmp_path
    stem = "测试卷"
    (series / f"{stem}.text.jsonl").write_text(
        _json.dumps({"page": "0001.jpg",
                     "regions": [{"bbox": [0, 0, 10, 10], "text": "台词"},
                                 {"bbox": [10, 0, 10, 10], "text": "咚"}]},
                    ensure_ascii=False) + "\n", encoding="utf-8")
    trans = tmp_path / "trans"
    trans.mkdir()
    (trans / "a.json").write_text(
        _json.dumps({"0001.jpg": {"0": "台词", "1": ""}}, ensure_ascii=False),
        encoding="utf-8")

    rc = audit_diff.main(["audit_diff.py", str(series), stem, "--trans-dir", str(trans),
                          "--max-report", "10"])
    assert rc == 0
    fix = _json.loads((series / f"{stem}.fix.json").read_text(encoding="utf-8"))
    assert fix["0001.jpg"]["1"] == ""
    assert "0" not in fix["0001.jpg"]


def test_clean_trans_strips_layout_noise():
    assert clean_trans.clean("不…不会吧　这…怎么　可能…？") == "不…不会吧这…怎么可能…？"
    assert clean_trans.clean("太棒了!") == "太棒了！"
    assert clean_trans.clean("：…过去?") == "…过去？"
