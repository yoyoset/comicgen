# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包脚本：生成 dist\comicgen\comicgen-ui.exe（文件夹版绿色软件）
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# PySide6(Qt6) 插件/数据 + llama_cpp 原生 DLL + OCR 词典/资源 + PDF/EasyOCR
for pkg in ("PySide6", "shiboken6", "llama_cpp", "unidic_lite", "fugashi", "manga_ocr",
            "fitz", "easyocr", "timm",
            "nvidia.cublas", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc",
            "nvidia.cufft", "nvidia.nvjitlink"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# transformers 懒加载模型族必须显式声明
hiddenimports += [
    "transformers.models.auto.image_processing_auto",
    "transformers.models.rt_detr_v2.modeling_rt_detr_v2",
    "transformers.models.rt_detr_v2.image_processing_rt_detr_v2",
    "transformers.models.rt_detr_v2.configuration_rt_detr_v2",
    "transformers.models.trocr.modeling_trocr",
    "transformers.models.trocr.configuration_trocr",
    "transformers.models.trocr.processing_trocr",
    "transformers.models.vision_encoder_decoder.modeling_vision_encoder_decoder",
    "transformers.models.vision_encoder_decoder.configuration_vision_encoder_decoder",
    "transformers.models.vit.modeling_vit",
    "manga_ocr",
    "simple_lama_inpainting",
    "easyocr",
    "timm",
    "rarfile",
]

a = Analysis(
    ["entry_ui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["gradio", "gradio_client", "webview", "proxy_tools", "groovy", "safehttpx",
              "matplotlib", "tkinter", "IPython", "jupyter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="comicgen-ui",
    console=False,  # 无黑窗；日志写 exe 同级 comicgen.log
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="comicgen",
)