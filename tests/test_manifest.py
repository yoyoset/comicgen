"""core.manifest 的离线测试：防止字段遗漏 / 精确字节被"顺手"改成整数估算值。"""
from comic_gen.core.manifest import LAYOUT_DIR, LAYOUT_FILE, MODEL_MANIFEST

EXPECTED_KEYS = {"detect", "ocr_ja", "ocr_other", "sakura", "llm", "lama"}

# 已实测核实的精确字节数，见 offline-and-model-sharing 项目 memory
EXPECTED_SIZES = {
    "sakura": 4_250_298_208,
    "llm": 2_497_281_120,
    "lama": 205_803_670,
}


def test_manifest_has_all_six_keys():
    assert set(MODEL_MANIFEST) == EXPECTED_KEYS


def test_key_field_matches_dict_key():
    for key, spec in MODEL_MANIFEST.items():
        assert spec.key == key


def test_layout_file_specs_have_required_fields():
    for key, spec in MODEL_MANIFEST.items():
        if spec.layout != LAYOUT_FILE:
            continue
        assert spec.filename, f"{key}: LAYOUT_FILE 必须有 filename"
        assert spec.size_bytes > 0, f"{key}: LAYOUT_FILE 必须有精确 size_bytes"
        assert spec.magic, f"{key}: LAYOUT_FILE 必须有 magic"
        assert spec.ext_filter, f"{key}: LAYOUT_FILE 必须有 ext_filter"


def test_layout_dir_specs_have_member_files():
    for key, spec in MODEL_MANIFEST.items():
        if spec.layout != LAYOUT_DIR:
            continue
        assert spec.member_files, f"{key}: LAYOUT_DIR 必须有 member_files"
        for group in spec.member_files:
            assert isinstance(group, tuple) and len(group) >= 1


def test_exact_sizes_match_measured_values():
    for key, expected in EXPECTED_SIZES.items():
        assert MODEL_MANIFEST[key].size_bytes == expected, (
            f"{key} 的 size_bytes 与实测值不符——不要把精确字节数改成估算的整数"
        )


def test_every_spec_has_purpose_and_label():
    for key, spec in MODEL_MANIFEST.items():
        assert spec.purpose, f"{key}: 缺少 purpose"
        assert spec.label, f"{key}: 缺少 label"


def test_ocr_ja_member_files_reject_the_split_snapshot():
    """实测踩过的坑：manga-ocr-base 有两个 snapshot 目录，一个只有 model.safetensors，
    另一个有 config/tokenizer 但没有权重文件。member_files 必须能正确拒绝任一半。"""
    spec = MODEL_MANIFEST["ocr_ja"]
    only_weights = {"model.safetensors"}
    only_config = {"config.json", "preprocessor_config.json", "vocab.txt",
                   "special_tokens_map.json", "tokenizer_config.json", "pytorch_model.bin"}

    def satisfied(present: set[str]) -> bool:
        return all(any(f in present for f in group) for group in spec.member_files)

    assert not satisfied(only_weights)
    assert satisfied(only_config)  # pytorch_model.bin 满足最后一组
