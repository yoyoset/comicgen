"""translate_view.list_preview_images 的离线测试：纯函数，不构造任何 Qt 控件。

只 import 模块（PySide6 的 QtCore/QtGui/QtWidgets import 本身不需要 QApplication），
不实例化 TranslateView——避免在本项目没有 UI 测试基建的情况下引入控件生命周期。
"""
from PIL import Image

from comic_gen.ui.views.translate_view import _PREVIEW_PAGE_SIZE, list_preview_images


def _make_output_folder(tmp_path, names):
    trans = tmp_path / "translated"
    trans.mkdir(parents=True)
    for name in names:
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            Image.new("RGB", (10, 10), "white").save(trans / name)
        else:
            (trans / name).write_text("not an image", encoding="utf-8")
    return tmp_path


def test_none_output_returns_empty():
    assert list_preview_images(None) == []


def test_missing_translated_dir_returns_empty(tmp_path):
    assert list_preview_images(str(tmp_path)) == []


def test_file_output_resolves_to_sibling_work_dir(tmp_path):
    """zip/PDF 类输入的 output 是文件，其工作目录是 <文件名去扩展>/（与 Runner._work_dir
    的命名规则一致）——预览必须从这里找 translated/。"""
    work = tmp_path / "book_translated"
    work.mkdir()
    trans = work / "translated"
    trans.mkdir()
    Image.new("RGB", (10, 10), "white").save(trans / "p1.png")

    out_file = tmp_path / "book_translated.zip"
    out_file.touch()  # 真实场景里打包完成后文件必然存在；is_file() 判定依赖这一点
    imgs = list_preview_images(str(out_file))
    assert [p.name for p in imgs] == ["p1.png"]


def test_natural_order_pages_instead_of_lexical(tmp_path):
    """page_2 必须在 page_10 之前——旧实现用 str 排序会把 page_10 排在 page_2 前面。"""
    out = _make_output_folder(tmp_path, ["page_10.png", "page_2.png", "page_1.png", "page_20.png"])
    assert [p.name for p in list_preview_images(str(out))] == [
        "page_1.png", "page_2.png", "page_10.png", "page_20.png",
    ]


def test_non_image_files_excluded(tmp_path):
    out = _make_output_folder(tmp_path, ["a.png", "b.jpg", "c.gif", "notes.txt"])
    assert [p.name for p in list_preview_images(str(out))] == ["a.png", "b.jpg"]


def test_preview_page_size_is_sane():
    assert 0 < _PREVIEW_PAGE_SIZE < 100  # 一次只加载一页，不能几百张全塞内存
