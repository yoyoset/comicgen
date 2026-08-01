r"""一致性检查：build_exe.spec 的 collect_all 清单（bundle_packages.py）里的每个包，
要么在 pyproject.toml 声明的依赖闭包里能追溯到、要么至少在当前 venv 里确实装着。

不是 CI 门禁（这个项目没有 CI），是发布/打包前手动跑一遍的只读诊断——和
scripts/diagnose_page.py 同类工具，不改任何东西，只报告。

之前 collect_all 清单是"观察到哪个包让 EXE 崩了就加进去"长出来的经验列表，没有
任何机制防止它跟着依赖变化腐化——实测就用这份检查抓到过一个真的死条目：`timm`
既不在 pyproject.toml 的依赖闭包里，这台机器也没装，collect_all 遇到它只会打一条
警告然后静默跳过，纯粹的历史遗留，已经删掉。

用法：.\.venv\Scripts\python.exe scripts\check_bundle_deps.py
"""
from __future__ import annotations

import importlib.metadata as md
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bundle_packages import COLLECT_ALL_PACKAGES  # noqa: E402

# collect_all 用的是 import 名，pyproject.toml 里的依赖声明用的是发行名（PyPI 包名）——
# 两者不总是一样（比如 pymupdf 这个发行名对应 fitz 这个 import 名）。已知的映射列在这里，
# 其余按"下划线转连字符"的常规规则去查。
_IMPORT_TO_DIST_NAME = {
    "fitz": "pymupdf",
    "manga_ocr": "manga-ocr",
    "llama_cpp": "llama-cpp-python",
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_dependency_closure() -> set[str]:
    """从 pyproject.toml 的 [project.dependencies] + 全部 optional-dependencies 出发，
    沿着已安装包的 Requires 元数据递归展开，得到"这些声明依赖实际会拉进来的全部包"。
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    roots = list(data["project"]["dependencies"])
    for group in data["project"].get("optional-dependencies", {}).values():
        roots += group

    closure: set[str] = set()
    queue = [re.split(r"[<>=!~\[]", r, maxsplit=1)[0].strip() for r in roots]
    while queue:
        name = queue.pop()
        key = _norm(name)
        if key in closure:
            continue
        closure.add(key)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue  # 声明了但这个环境里没装（比如平台限定的可选依赖），不继续展开
        for r in dist.requires or []:
            if "extra ==" in r:
                continue
            pkg = re.split(r"[ ;<>=!~\[]", r, maxsplit=1)[0].strip()
            if pkg:
                queue.append(pkg)
    return closure


def main() -> int:
    closure = _declared_dependency_closure()
    problems: list[str] = []

    for pkg in COLLECT_ALL_PACKAGES:
        dist_name = _IMPORT_TO_DIST_NAME.get(pkg, pkg)
        try:
            md.distribution(dist_name)
            installed = True
        except md.PackageNotFoundError:
            installed = False

        if not installed:
            problems.append(f"{pkg}：当前 venv 没有安装——collect_all 会静默跳过，"
                             f"打包出的 EXE 缺这部分数据/DLL，而不是报错")
        elif _norm(dist_name) not in closure:
            problems.append(f"{pkg}：装着，但从 pyproject.toml 声明的依赖追不到——"
                             f"是不是该从 bundle_packages.py 里删掉了？")

    if problems:
        print(f"build_exe.spec 的 collect_all 清单有 {len(problems)} 处可能过期：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"collect_all 清单里的 {len(COLLECT_ALL_PACKAGES)} 个包全部已安装，"
          f"且都能追溯到 pyproject.toml 声明的依赖。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
