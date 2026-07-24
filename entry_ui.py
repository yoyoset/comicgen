"""PyInstaller 入口脚本：绝对导入，避免包内相对导入在冻结环境下失败。"""
from comic_gen.ui.app import main

if __name__ == "__main__":
    main()
