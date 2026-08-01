# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包脚本：生成 dist\comicgen\comicgen-ui.exe（文件夹版绿色软件）
from PyInstaller.utils.hooks import collect_all

from bundle_packages import COLLECT_ALL_PACKAGES

datas, binaries, hiddenimports = [], [], []

# 清单定义在 bundle_packages.py（和 scripts/check_bundle_deps.py 共用同一份，
# 不再各写一份列表——这正是之前"两份清单没有东西保证一致"的根源）。
#
# PySide6/shiboken6 不在这里：collect_all("PySide6") 会强行分析 Qt 全家桶（Qt3D/QML/
# WebEngine/Bluetooth…），代码实际只 import QtCore/QtGui/QtWidgets 三个模块（grep 可证）。
# PyInstaller 自带的 hook-PySide6.py 只要求 shiboken6 + 少量核心 Qt 运行时 DLL，具体
# Qt 子模块由静态导入分析驱动——去掉 collect_all 后，正常的 Analysis 阶段就只会
# 触发 hook-PySide6.QtCore/QtGui/QtWidgets 这三个 hook，不需要手工列 hiddenimports。
#
# nvidia.* 也不在这里：那是给 llama-cpp-python 找 CUDA 运行时用的独立 pip 包
# （nvidia-cublas-cu12 等），但 torch/lib 下已经自带同一套 DLL（cublas/cublasLt/cufft/
# cufftw/nvrtc/nvJitLink/cudart），是重复的第二套 CUDA 栈。_cuda_runtime.py 现在改为
# 指向 torch/lib，不再需要单独打包 nvidia.* 那五个包。
for pkg in COLLECT_ALL_PACKAGES:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# transformers 懒加载模型族必须显式声明
hiddenimports += [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
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
    "rarfile",
]

# 代码只用 QtCore/QtGui/QtWidgets；显式排除大头 Qt 子模块，防止任何间接依赖不小心
# 把它们拽进来（例如某个第三方包意外 import 了 QtSvg 之类）。collect_all 去掉之后
# 这份 excludes 严格说不是必需的，留着是防御性的、也是给后来人看"这些模块确定不需要"。
_qt_excludes = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtAxContainer", "PySide6.QtBluetooth", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtDesigner", "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets", "PySide6.QtHelp", "PySide6.QtHttpServer",
    "PySide6.QtLocation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork", "PySide6.QtNetworkAuth", "PySide6.QtNfc",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtPrintSupport",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialBus",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
    "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtXml",
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
              "matplotlib", "tkinter", "IPython", "jupyter"] + _qt_excludes,
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