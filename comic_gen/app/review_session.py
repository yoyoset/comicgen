"""校对会话：读写 pages.json + 单页重渲染。Qt-free，供 ui/views/review_view.py 调用。

之前"校对"约等于退出应用、找到从没在界面上出现过的 pages.json、在文本编辑器里
对着坐标数组猜哪个区域对应哪个气泡、手改 translated_text，再回来点"重渲染"（那是
整卷 inpaint+render 全部重跑一遍）。这里给 UI 一个能列出"需要注意"的区域、编辑单个
区域译文、只重渲染这一页的会话对象，不用再猜坐标、不用为改一个字重跑一整卷。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..core.config import Config
from ..core.errors import InputError
from ..core.job import load_pages, read_original_input, save_pages
from ..core.models import STATUS_RENDERED, STATUS_TRANSLATED, Page
from ..core.utils import atomic_save_image
from ..infra.engine_cache import EngineCache

# 检测置信度低于这个值时提示"需要注意"。纯展示阈值，不影响流水线的 suppress_overlaps
# 判定——和 diagnose_page.py 里给用户看的诊断阈值同一个数量级，没有实测校准过。
LOW_SCORE_THRESHOLD = 0.5


def load_config_for_review(work_dir: str | Path, fallback: Config) -> Config:
    """校对/单页重渲染要用产生这批结果时的原始配置（模型路径绑定、设备、字体、
    检测阈值……），不是"现在设置页里恰好显示的值"——两者随时可能不一致（比如打开
    一个不是本次应用会话跑出来的旧工作目录）。job.json（Phase 2.3）已经存了完整
    快照，没有的话（旧数据/被删）才退回调用方传入的兜底值。
    """
    job_json = Path(work_dir) / "job.json"
    if not job_json.exists():
        return fallback
    try:
        data = json.loads(job_json.read_text(encoding="utf-8"))
        return Config(**data["config"])
    except Exception:
        return fallback


@dataclass
class AttentionItem:
    page_image: str
    region_index: int  # 页面级问题（如整页失败）用 -1，不对应具体某个区域
    reason: str


class ReviewSession:
    """一次打开某个工作目录进行校对。构造时会重新解析一遍原始输入定位原图——
    和"跨会话重渲染"（main_window._on_rerender 的回退分支）付出的是同一份代价，
    不是这里新引入的开销。
    """

    def __init__(self, work_dir: str | Path, cfg: Config):
        self.work_dir = Path(work_dir)
        self.cfg = cfg
        self.pages_json = self.work_dir / "pages.json"
        if not self.pages_json.exists():
            raise InputError(
                f"这个目录里没有 pages.json，不是一个有效的翻译工作目录：{self.work_dir}",
                technical=str(self.pages_json),
            )
        self.pages, self._input_fingerprint = load_pages(self.pages_json)
        self._by_image: dict[str, Page] = {p.image: p for p in self.pages}

        original_input = read_original_input(self.pages_json)
        if not original_input:
            raise InputError(
                "pages.json 中缺少原始输入记录，无法定位原图进行校对。",
                technical=str(self.pages_json),
            )
        from ..pipeline.loader import load_input

        loaded = load_input(original_input, self.work_dir / "_extract", cfg)
        self._orig_map: dict[str, Path] = dict(zip(loaded.rel_names, loaded.pages))
        self._inpainter_cache: EngineCache = EngineCache()

    # ------------------------------------------------------------ 只读
    def page_images(self) -> list[str]:
        return [p.image for p in self.pages]

    def get_page(self, image: str) -> Page:
        return self._by_image[image]

    def preview_path(self, image: str) -> Path:
        """校对画布展示用的图：已经渲染过就用渲染结果，否则退回原图（还没轮到这一页
        或这一页处理失败），不能因为还没渲染就在画布上什么都不显示。
        """
        rendered = self.work_dir / "translated" / image
        if rendered.exists():
            return rendered
        return self._orig_map[image]

    def attention_items(self) -> list[AttentionItem]:
        """需要注意的区域：检测置信度低 / OCR 没识别出文字 / 有原文没译文，外加整页
        级别的失败。不用通读全部页面就能直奔真正需要改的地方。
        """
        items: list[AttentionItem] = []
        for p in self.pages:
            for i, r in enumerate(p.regions):
                if r.score and r.score < LOW_SCORE_THRESHOLD:
                    items.append(AttentionItem(p.image, i, f"检测置信度低（{r.score:.2f}）"))
                elif not r.source_text.strip():
                    items.append(AttentionItem(p.image, i, "OCR 未识别出文字"))
                elif not r.translated_text.strip():
                    items.append(AttentionItem(p.image, i, "缺少译文"))
            if p.error:
                items.append(AttentionItem(p.image, -1, f"{p.error_stage or '未知阶段'}：{p.error}"))
        return items

    # ------------------------------------------------------------ 编辑
    def update_region_text(self, image: str, region_index: int, translated_text: str) -> None:
        page = self._by_image[image]
        page.regions[region_index].translated_text = translated_text
        # 改完译文后这一页要在下次"导出"（Runner.rerender）时被重新渲染，不能停在
        # rendered 状态被 todo 过滤器跳过——语义上和 Runner.rerender 强制把 rendered
        # 打回 translated 是一回事。
        if page.status == STATUS_RENDERED:
            page.status = STATUS_TRANSLATED

    def save(self) -> None:
        save_pages(self.pages_json, self.pages, self._input_fingerprint)

    # ------------------------------------------------------------ 单页重渲染
    def _get_inpainter(self):
        from ..pipeline.inpaint import Inpainter

        sig = (self.cfg.device, self.cfg.model_paths.get("lama"))

        def factory():
            return Inpainter(self.cfg.device, model_path=self.cfg.model_paths.get("lama"))

        return self._inpainter_cache.get_or_rebuild(sig, factory)

    def rerender_page(self, image: str) -> Path:
        """只重渲染这一页（抹字+渲染），不碰其它页、不重新打包最终产物——重新打包是
        "导出"（Runner.rerender）的职责，改一个气泡不该触发整卷重新打包。
        """
        from ..pipeline.runner import render_single_page

        page = self._by_image[image]
        orig_path = self._orig_map.get(image)
        if orig_path is None:
            raise InputError(f"找不到原图：{image}", technical=f"not in orig_map: {image}")

        img = Image.open(orig_path).convert("RGB")
        inpainter = self._get_inpainter()
        out_img = render_single_page(img, page, self.cfg, inpainter)
        out_path = self.work_dir / "translated" / page.image
        # 原子写图：和 Runner 批量渲染用同一份实现，进程被杀不留下截断 PNG
        atomic_save_image(out_img, out_path)
        page.status = STATUS_RENDERED
        page.error = ""
        page.error_stage = ""
        self.save()
        return out_path
