"""应用层：面向脚本/CLI/测试的任务入口，不依赖 Qt。

`ui/job_controller.py` 出于跨任务复用引擎缓存的需要，持有一个长期存活的
`Runner` 实例（改设置不用重启整个应用）。这里给的是更简单的一次性入口——每次
调用建一个新 `Runner`，跑完即弃，适合脚本/测试/未来的批量 CLI 场景，不需要关心
引擎缓存的生命周期。`ui/job_controller.py` 不改为走这里——它的"复用 Runner"
是有意的性能设计，硬套成每次新建反而是倒退。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..core.config import Config
from ..core.errors import format_job_failure
from ..core.job import JobReport
from ..pipeline.registry import SUPPORTED_SOURCE_LANGS, TRANSLATOR_REGISTRY
from ..pipeline.runner import Runner


def run_job(
    input_path: str | Path,
    output_path: str | Path | None = None,
    cfg: Config | None = None,
    log: Callable[[str], None] = print,
    should_cancel: Callable[[], bool] | None = None,
) -> JobReport:
    """跑一次翻译任务，返回 JobReport。"""
    return Runner(cfg, log=log).run(input_path, output_path, should_cancel=should_cancel)


def rerender_job(
    input_path: str | Path,
    output_path: str | Path | None = None,
    cfg: Config | None = None,
    log: Callable[[str], None] = print,
) -> JobReport:
    """重渲染（译后编辑）：pages.json 里 translated_text 改过之后重新抹字+渲染。"""
    return Runner(cfg, log=log).rerender(input_path, output_path)


def _build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m comic_gen.app.job_service",
        description="comic-gen 命令行入口：跑一次翻译任务，不需要 UI。",
    )
    p.add_argument("input", help="输入路径：文件夹 / ZIP / CBZ / RAR / CBR / EPUB / PDF")
    p.add_argument("-o", "--output", default=None, help="输出路径，留空按默认规则推导")
    # choices 从 pipeline.registry 推导而不是这里再写一份硬编码列表——加新引擎/新
    # 语言只改注册表，CLI 自动跟上，不会出现"注册表能跑但 CLI 传不进去"的漂移。
    p.add_argument("--translator", default="sakura", choices=tuple(TRANSLATOR_REGISTRY))
    p.add_argument("--source-lang", default="ja", choices=SUPPORTED_SOURCE_LANGS)
    p.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    cfg = Config(translator=args.translator, source_lang=args.source_lang, device=args.device)
    try:
        report = run_job(args.input, args.output, cfg)
    except Exception as e:
        # 和 ui/workers.py 用同一套 format_job_failure：ComicGenError 只打印人话，
        # 其它未预期异常仍然带完整 traceback（真程序 bug 需要能诊断）。
        print(format_job_failure(e))
        return 1
    print(report.summary)
    return 0 if report.status in ("ok", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
