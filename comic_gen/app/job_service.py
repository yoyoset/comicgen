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
    p.add_argument("--cleanup-intermediate", action="store_true",
                   help="任务完整成功后删除中间产物，仅保留最终交付物（之后无法校对/断点续跑）")
    p.add_argument("--glossary", default=None, metavar="FILE",
                   help="专有名词译名表（UTF-8，每行「原文=译名」，# 开头为注释），例如人名统一"
                        "译法。见 comic_gen/pipeline/glossary.py")
    # 图片输入的"处理分辨率"：长边按参考开本(B5)折算到这个 dpi 再进流水线，超出的图会先缩小。
    # 300 dpi 适合 300ppi 的阅读器/放大看细节，代价是检测/OCR/抹字耗时随像素数近似线性上涨；
    # 150 是默认值，够 6-7 寸屏。
    p.add_argument("--pdf-dpi", type=int, default=150, metavar="DPI",
                   help="处理分辨率（图片输入按 B5 长边折算，默认 150；300 适合 300ppi 阅读器）")
    # 字号上限跟处理分辨率联动：300 dpi 下页面像素翻倍，仍按 48px 上限排版会让字只有一半大。
    # 只暴露这两个旋钮，不搞自动缩放——自动缩放会让 150/300 的输出不可预期地漂移。
    p.add_argument("--min-font-size", type=int, default=10, metavar="PX",
                   help="译文最小字号（默认 10；300 dpi 建议 20）")
    p.add_argument("--max-font-size", type=int, default=48, metavar="PX",
                   help="译文最大字号（默认 48；300 dpi 建议 96）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    glossary: dict[str, str] = {}
    if args.glossary:
        from ..pipeline.glossary import parse_glossary_text

        path = Path(args.glossary)
        if not path.is_file():
            print(f"术语表文件不存在：{path}")
            return 1
        glossary = parse_glossary_text(path.read_text(encoding="utf-8"))
        if not glossary:
            print(f"术语表文件里没有解析出任何条目（格式应为每行「原文=译名」）：{path}")
            return 1
        print(f"术语表：{len(glossary)} 条（{path}）")
    if args.min_font_size > args.max_font_size:
        print(f"最小字号不能大于最大字号（{args.min_font_size} > {args.max_font_size}）")
        return 1
    cfg = Config(translator=args.translator, source_lang=args.source_lang, device=args.device,
                 cleanup_intermediate=args.cleanup_intermediate, glossary=glossary,
                 pdf_dpi=args.pdf_dpi, min_font_size=args.min_font_size,
                 max_font_size=args.max_font_size)
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
