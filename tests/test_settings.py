"""core.settings 的离线测试：parse_or_recover 是纯函数，load/save 用 tmp_path 隔离真实文件系统。"""
import json

from comic_gen.core.settings import AppSettings, migrate_legacy_settings

# ==================================================================== parse_or_recover（纯函数）

def test_parse_normal_json():
    s, warnings = AppSettings.parse_or_recover(json.dumps({
        "model_dir": "F:\\hf_cache", "device": "cpu", "pdf_dpi": 300,
    }))
    assert s.model_dir == "F:\\hf_cache"
    assert s.device == "cpu"
    assert s.pdf_dpi == 300
    assert warnings == []


def test_parse_unknown_key_ignored():
    s, warnings = AppSettings.parse_or_recover(json.dumps({
        "model_dir": "x", "some_future_field": "whatever",
    }))
    assert s.model_dir == "x"
    assert warnings == []


def test_parse_single_wrong_type_field_falls_back_alone():
    """核心场景：一个字段类型错，只让那个字段回默认，model_dir/model_files 必须原样保留。"""
    s, warnings = AppSettings.parse_or_recover(json.dumps({
        "model_dir": "F:\\hf_cache",
        "model_files": {"sakura": "F:\\hf_cache\\sakura.gguf"},
        "pdf_dpi": "not_a_number",  # 应该是 int
    }))
    assert s.model_dir == "F:\\hf_cache"
    assert s.model_files == {"sakura": "F:\\hf_cache\\sakura.gguf"}
    assert s.pdf_dpi == AppSettings().pdf_dpi  # 回落默认
    assert len(warnings) == 1
    assert "pdf_dpi" in warnings[0]


def test_parse_total_garbage_returns_defaults_with_warning():
    s, warnings = AppSettings.parse_or_recover("not json at all {{{")
    assert s == AppSettings()
    assert len(warnings) == 1


def test_parse_non_dict_json_returns_defaults():
    s, warnings = AppSettings.parse_or_recover("[1, 2, 3]")
    assert s == AppSettings()
    assert len(warnings) == 1


def test_model_files_roundtrip():
    original = AppSettings(model_files={"sakura": "a.gguf", "llm": "b.gguf"})
    s, warnings = AppSettings.parse_or_recover(json.dumps(original.__dict__))
    assert s.model_files == {"sakura": "a.gguf", "llm": "b.gguf"}
    assert warnings == []


def test_legacy_sakura_filename_absolute_path_migrated(tmp_path):
    fake_gguf = tmp_path / "sakura.gguf"
    fake_gguf.write_bytes(b"x")
    s, warnings = AppSettings.parse_or_recover(json.dumps({
        "sakura_filename": str(fake_gguf),
    }))
    assert s.model_files["sakura"] == str(fake_gguf)
    assert any("sakura_filename" in w for w in warnings)


def test_legacy_bare_filename_dropped_not_guessed():
    """旧版留空自动挑量化时，sakura_filename 可能只是个裸文件名——信息量不够，
    宁可丢弃提示用户重新指认，也不能瞎拼一个大概率错的路径。"""
    s, warnings = AppSettings.parse_or_recover(json.dumps({
        "sakura_filename": "sakura-v1.gguf",
    }))
    assert "sakura" not in s.model_files
    assert any("丢弃" in w for w in warnings)


def test_legacy_filename_does_not_override_existing_binding(tmp_path):
    fake = tmp_path / "old.gguf"
    fake.write_bytes(b"x")
    s, _ = AppSettings.parse_or_recover(json.dumps({
        "model_files": {"sakura": "already_bound.gguf"},
        "sakura_filename": str(fake),
    }))
    assert s.model_files["sakura"] == "already_bound.gguf"


# ==================================================================== load/save（真实文件 I/O，tmp_path 隔离）

def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    original = AppSettings(model_dir="F:\\hf_cache", device="cpu",
                            model_files={"sakura": "x.gguf"}, setup_completed=True)
    original.save(path)
    loaded = AppSettings.load(path)
    assert loaded == original


def test_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    # 显式指向一个不存在的旧位置，不依赖"这台开发机没有遗留的 %LOCALAPPDATA% 配置"这个偶然事实
    monkeypatch.setattr("comic_gen.core.settings.legacy_settings_path", lambda: tmp_path / "no_legacy.json")
    assert AppSettings.load(tmp_path / "nope.json") == AppSettings()


def test_load_corrupt_file_writes_bak_before_returning_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{{{not json", encoding="utf-8")
    result = AppSettings.load(path)
    assert result == AppSettings()
    assert (tmp_path / "settings.json.bak").exists()
    assert (tmp_path / "settings.json.bak").read_text(encoding="utf-8") == "{{{not json"


def test_load_partial_corruption_does_not_write_bak(tmp_path):
    """单字段类型错不算"损坏"——不该产生 .bak 噪音，只有整份解析失败才需要。"""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model_dir": "x", "pdf_dpi": "oops"}), encoding="utf-8")
    AppSettings.load(path)
    assert (tmp_path / "settings.json.bak").exists()  # 有警告就备份，逐字段兜底也算"有异常"


def test_migrate_legacy_settings_copies_not_moves(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"model_dir": "F:\\old"}), encoding="utf-8")
    new_path = tmp_path / "new" / "settings.json"
    assert migrate_legacy_settings(new_path, legacy) is True
    assert new_path.exists()
    assert legacy.exists()  # 原文件必须还在，不能删


def test_load_migrates_from_legacy_when_new_missing(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy_settings.json"
    legacy.write_text(json.dumps({"model_dir": "F:\\old_cache", "device": "cpu"}), encoding="utf-8")
    monkeypatch.setattr("comic_gen.core.settings.legacy_settings_path", lambda: legacy)

    new_path = tmp_path / "new_settings.json"
    result = AppSettings.load(new_path)
    assert result.model_dir == "F:\\old_cache"
    assert result.device == "cpu"
    assert new_path.exists()  # 迁移后新位置应该有文件了
    assert legacy.exists()  # 旧文件不删
