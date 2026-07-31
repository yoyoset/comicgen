"""core.job.build_job_snapshot 的纯函数测试（不做 IO）。"""
import json

from comic_gen.core.config import Config
from comic_gen.core.job import build_job_snapshot


def test_build_job_snapshot_shape():
    cfg = Config(translator="sakura", source_lang="ja", det_threshold=0.4)
    snapshot = build_job_snapshot(
        cfg,
        app_version="0.1.0",
        resolved_model_paths={"detect": "F:/hf_cache/detect.safetensors"},
        stages={"detect": {"duration_s": 1.2, "page_count": 3}},
    )
    assert snapshot["config"]["translator"] == "sakura"
    assert snapshot["config"]["det_threshold"] == 0.4
    assert snapshot["app_version"] == "0.1.0"
    assert snapshot["resolved_model_paths"] == {"detect": "F:/hf_cache/detect.safetensors"}
    assert snapshot["stages"]["detect"]["page_count"] == 3


def test_build_job_snapshot_is_json_serializable():
    """事后诊断要能直接落盘/读回，config 里任何字段都不能是不可序列化的对象。"""
    cfg = Config()
    snapshot = build_job_snapshot(cfg, "0.1.0", {}, {})
    json.dumps(snapshot)  # 不应抛异常


def test_build_job_snapshot_copies_mutable_args():
    """resolved_model_paths/stages 传进来的字典不该被后续外部修改影响到快照本身。"""
    paths = {"detect": "x"}
    stages = {"detect": {}}
    snapshot = build_job_snapshot(Config(), "0.1.0", paths, stages)
    paths["detect"] = "changed"
    stages["ocr"] = {}
    assert snapshot["resolved_model_paths"] == {"detect": "x"}
    assert "ocr" not in snapshot["stages"]


def test_build_job_snapshot_empty_stages_and_paths():
    snapshot = build_job_snapshot(Config(), "0.1.0", {}, {})
    assert snapshot["resolved_model_paths"] == {}
    assert snapshot["stages"] == {}
