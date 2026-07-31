"""核心数据模型：页面（Page）与文本区域（Region）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# 页面处理状态（按流水线阶段递进）
STATUS_PENDING = "pending"
STATUS_DETECTED = "detected"
STATUS_OCRED = "ocred"
STATUS_TRANSLATED = "translated"
STATUS_RENDERED = "rendered"

DIRECTION_VERTICAL = "vertical"
DIRECTION_HORIZONTAL = "horizontal"


@dataclass
class Region:
    bbox: list[int]  # [x, y, w, h]
    direction: str = DIRECTION_VERTICAL  # vertical | horizontal
    source_text: str = ""
    translated_text: str = ""
    color: list[int] = field(default_factory=lambda: [0, 0, 0])
    kind: str = "text"  # text_bubble | text_free
    score: float = 0.0  # 检测置信度，0.0 = 未知/旧数据（不参与去重判定的默认值）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Region":
        keys = ("bbox", "direction", "source_text", "translated_text", "color", "kind", "score")
        return cls(**{k: d[k] for k in keys if k in d})


@dataclass
class Page:
    image: str  # 相对路径（相对输入根 / 解包根）
    width: int = 0
    height: int = 0
    regions: list[Region] = field(default_factory=list)
    status: str = STATUS_PENDING
    error: str = ""
    # 失败发生在哪个阶段（core.errors.Stage 的字符串值）。之前 JobReport 靠切开 error
    # 的中文消息找冒号猜阶段，脆且间接；这个字段由 runner 在对应阶段的 except 里直接写，
    # 不用猜。旧 pages.json 没有这个字段，默认空串，JobReport 那边有兜底逻辑。
    error_stage: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Page":
        d = dict(d)
        d["regions"] = [Region.from_dict(r) for r in d.get("regions", [])]
        return cls(**d)
