"""build_exe.spec 用 collect_all 兜底的包清单——单一定义，scripts/check_bundle_deps.py
和 build_exe.spec 都从这里导入。两边各写一份列表正是 Phase 4 审计指出的问题根源
（"没有任何东西保证两份清单一致"，实测就在这份清单里抓到一个真的死条目：timm
不在 pyproject.toml 的依赖闭包里、这台机器也没装，collect_all 遇到它只会静默跳过，
既不报错也不起作用，纯粹的历史遗留）。

只有 llama_cpp（GGUF 推理原生 DLL）、OCR 词典/资源、PDF 这几个包的资源文件不是
常规 Python 模块，PyInstaller 的静态导入分析找不到，需要 collect_all 兜底。
PySide6/shiboken6/nvidia.* 不在这里——理由见 build_exe.spec 里的注释。
"""

COLLECT_ALL_PACKAGES = (
    "llama_cpp",    # GGUF 推理原生 DLL，可选依赖 local-llm（pyproject.toml）
    "unidic_lite",  # manga-ocr 分词用的词典数据文件，manga-ocr 的传递依赖
    "fugashi",      # manga-ocr 的分词器，同样需要靠 collect_all 带上数据文件
    "manga_ocr",    # 日文 OCR，直接依赖
    "fitz",         # pymupdf 的导入名，直接依赖
    "easyocr",      # 英/中 OCR，直接依赖
)
