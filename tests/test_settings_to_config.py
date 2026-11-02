"""AppSettings.to_config()：全项目唯一的 Config 组装点，确保字段传递正确且是快照而非别名。"""
from comic_gen.core.job import JobRequest
from comic_gen.core.settings import AppSettings


def _request(**kw):
    return JobRequest(input_path="in.zip", **kw)


def test_to_config_carries_model_paths():
    s = AppSettings(model_files={"sakura": "F:\\hf_cache\\sakura.gguf", "llm": "F:\\hf_cache\\qwen3.gguf"})
    cfg = s.to_config(_request(engine="sakura", source_lang="ja"))
    assert cfg.model_paths == {"sakura": "F:\\hf_cache\\sakura.gguf", "llm": "F:\\hf_cache\\qwen3.gguf"}


def test_to_config_is_a_snapshot_not_an_alias():
    """事后改 settings.model_files 不能影响已经返回的 Config——否则一次设置改动
    可能在两次懒加载之间，把正在跑的任务的模型悄悄换掉。"""
    s = AppSettings(model_files={"sakura": "old.gguf"})
    cfg = s.to_config(_request(engine="sakura", source_lang="ja"))

    s.model_files["sakura"] = "new.gguf"
    s.model_files["llm"] = "added_later.gguf"

    assert cfg.model_paths == {"sakura": "old.gguf"}


def test_to_config_carries_request_fields():
    s = AppSettings()
    cfg = s.to_config(_request(engine="llm", source_lang="en", direction="vertical"))
    assert cfg.translator == "llm"
    assert cfg.source_lang == "en"
    assert cfg.direction == "vertical"


def test_to_config_carries_render_preferences():
    s = AppSettings(pdf_dpi=300, font_path="C:\\f.ttf", min_font_size=8, max_font_size=60,
                     det_threshold=0.5, device="cpu")
    cfg = s.to_config(_request())
    assert cfg.pdf_dpi == 300
    assert cfg.font_path == "C:\\f.ttf"
    assert cfg.min_font_size == 8
    assert cfg.max_font_size == 60
    assert cfg.det_threshold == 0.5
    assert cfg.device == "cpu"


def test_to_config_carries_cleanup_intermediate():
    s = AppSettings(cleanup_intermediate=True)
    cfg = s.to_config(_request())
    assert cfg.cleanup_intermediate is True
    assert AppSettings().to_config(_request()).cleanup_intermediate is False
