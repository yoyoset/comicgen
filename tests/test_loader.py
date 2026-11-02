"""Tests for pipeline.loader (offline, minimal data)."""
import tempfile
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from comic_gen.core.errors import InputError
from comic_gen.pipeline.loader import (
    LoadedInput,
    downscaled_size,
    load_input,
    pack_output,
    target_long_edge_px,
)


def create_test_images(tmpdir: Path, count: int = 2):
    """Helper to create test PNG images."""
    imgs = []
    for i in range(count):
        img = Image.new("RGB", (100, 150), color=(255, 255, 255))
        path = tmpdir / f"page_{i+1:03d}.png"
        img.save(path)
        imgs.append(path)
    return imgs


def test_pack_output_folder():
    """Test packing output to folder."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir)

        # Create LoadedInput for folder
        loaded = LoadedInput(
            kind="folder",
            input_path=tmpdir,
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        # Copy test images to translated dir
        for img_path in imgs:
            (trans_dir / img_path.name).write_bytes(img_path.read_bytes())

        output_dir = tmpdir / "output"
        result = pack_output(loaded, trans_dir, output_dir)

        # Check output files exist
        assert result.exists()
        assert (result / "page_001.png").exists()
        assert (result / "page_002.png").exists()


def test_pack_output_folder_removes_stale_images_from_old_run():
    """输出目录复用旧任务时，不属于当前输入的旧图必须被清掉——否则交付物里残留
    上一轮不同输入/换过输入后的旧页（deliverable 页数比当前输入多），是"污染旧任务
    目录"的经典问题。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir, count=2)

        loaded = LoadedInput(
            kind="folder", input_path=tmpdir, work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"], pages=[],
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        for img_path in imgs:
            (trans_dir / img_path.name).write_bytes(img_path.read_bytes())

        output_dir = tmpdir / "output"
        output_dir.mkdir()
        # 模拟旧任务残留：根目录多了 3 个不属于当前输入的旧图 + 一个非图片文件
        (output_dir / "page_003.png").write_bytes(b"stale")
        (output_dir / "page_004.jpg").write_bytes(b"stale")
        (output_dir / "notes.txt").write_bytes(b"keep")

        pack_output(loaded, trans_dir, output_dir)

        assert (output_dir / "page_001.png").exists()
        assert (output_dir / "page_002.png").exists()
        assert not (output_dir / "page_003.png").exists(), "旧页必须被清掉"
        assert not (output_dir / "page_004.jpg").exists(), "旧 jpg 同样要清"
        assert (output_dir / "notes.txt").exists(), "非图片文件不能碰"


def test_pack_output_folder_keeps_intermediate_dirs_untouched():
    """清理旧图时绝不能动 translated/ 与 _extract/ 这些中间产物目录里的文件。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir, count=1)

        loaded = LoadedInput(
            kind="folder", input_path=tmpdir, work_dir=tmpdir,
            rel_names=["page_001.png"], pages=[],
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(imgs[0].read_bytes())

        output_dir = tmpdir / "output"
        output_dir.mkdir()
        (output_dir / "translated").mkdir()
        (output_dir / "translated" / "page_003.png").write_bytes(b"intermediate")
        (output_dir / "_extract").mkdir()
        (output_dir / "_extract" / "old.png").write_bytes(b"extract")

        pack_output(loaded, trans_dir, output_dir)

        assert (output_dir / "translated" / "page_003.png").exists(), "translated/ 里的中间产物不能删"
        assert (output_dir / "_extract" / "old.png").exists(), "_extract/ 里的解包产物不能删"


def test_pack_output_zip():
    """Test packing output to ZIP."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        for img_path in imgs:
            (trans_dir / img_path.name).write_bytes(img_path.read_bytes())

        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip)

        # Check ZIP exists and contains files
        assert result.exists()
        assert zipfile.is_zipfile(result)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" in names


def test_load_input_missing_zip_raises_input_error(tmp_path):
    """回归测试：实测复现过——缺失的 zip 之前是裸 FileNotFoundError 一路抛到 CLI
    最外层，打印一整段 Python 堆栈。改成 InputError，人话+原始异常都保留。
    路径本身不存在时由存在性预检先拦下，报"输入路径不存在"——比"无法打开输入文件"
    更准确地指出问题是路径打错/文件被移走，而不是文件损坏。
    """
    missing = tmp_path / "does_not_exist.zip"
    with pytest.raises(InputError, match="输入路径不存在"):
        load_input(missing, tmp_path / "_extract")


def test_load_input_corrupt_zip_raises_input_error(tmp_path):
    bad_zip = tmp_path / "corrupt.zip"
    bad_zip.write_bytes(b"this is not a real zip file")
    with pytest.raises(InputError, match="无法打开输入文件"):
        load_input(bad_zip, tmp_path / "_extract")


def test_load_input_nonexistent_path_is_not_unsupported_type(tmp_path):
    """路径不存在（打错字）必须报"路径不存在"，不能伪装成"不支持的输入类型"——
    后者的排查方向（改格式）对这个问题完全无效。无后缀路径最容易触发这个回归。"""
    missing = tmp_path / "no_such_folder"
    with pytest.raises(InputError, match="输入路径不存在"):
        load_input(missing, tmp_path / "_extract")


def test_pack_output_missing_images_and_no_original_are_skipped():
    """既没有译文也没有可回退的原图（pages=[] 意味着 loader 层根本没有该页的原始
    路径记录）时，只能跳过——这是真正无解的情况，和"有原图但翻译失败"是两回事。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir, count=1)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],  # page_002 无译文也无原图记录
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        # Only copy page_001
        (trans_dir / "page_001.png").write_bytes(imgs[0].read_bytes())

        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip)

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" not in names


def test_pack_output_falls_back_to_original_when_translation_missing():
    """0.4 回归测试：某页翻译失败、trans_dir 里没有它，但原图还在——打包时必须用
    原图顶上，而不是让这一页从产物里消失（旧行为：直接跳过，产物比原作少页）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        orig_dir = tmpdir / "orig"
        orig_dir.mkdir()
        orig_imgs = create_test_images(orig_dir, count=2)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],
            pages=orig_imgs,  # 两页的原始路径都在，第二页从未成功翻译
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        # 只有第一页翻译成功；第二页失败，trans_dir 里没有它
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())

        logs = []
        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip, log=logs.append)

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" in names  # 用原图顶上了，不再从产物里消失

        assert any("page_002.png" in m and "原图" in m for m in logs)
        assert not any("page_001.png" in m for m in logs)  # 成功的页不该被当成回退


def test_pack_output_folder_falls_back_to_original():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        orig_dir = tmpdir / "orig"
        orig_dir.mkdir()
        orig_imgs = create_test_images(orig_dir, count=2)

        loaded = LoadedInput(
            kind="folder", input_path=tmpdir, work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"], pages=orig_imgs,
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())

        output_dir = tmpdir / "output"
        result = pack_output(loaded, trans_dir, output_dir)
        assert (result / "page_001.png").exists()
        assert (result / "page_002.png").exists()  # 回退原图


def test_pack_output_pdf_falls_back_to_original():
    """_pack_pdf 是独立的重打包路径（不复用 pack_output 主体的分支），单独验证回退。"""
    pytest.importorskip("fitz")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        orig_dir = tmpdir / "src"
        orig_dir.mkdir()
        orig_imgs = create_test_images(orig_dir, count=2)

        loaded = LoadedInput(
            kind="pdf", input_path=tmpdir / "in.pdf", work_dir=orig_dir,
            rel_names=["page_001.png", "page_002.png"], pages=orig_imgs,
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())

        output_pdf = tmpdir / "output.pdf"
        result = pack_output(loaded, trans_dir, output_pdf, pdf_dpi=150)

        assert result.exists()
        import fitz

        with fitz.open(result) as doc:
            assert doc.page_count == 2  # 第二页没丢，用原图撑起来了


def test_pack_output_epub_falls_back_to_original_without_self_copy_error():
    """epub 回退时原图本来就在 work_dir（staging）里，不能对同一个文件 copy 到自己，
    否则会抛 SameFileError；这里验证不崩。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        staging = tmpdir / "staging"
        staging.mkdir()
        orig_imgs = create_test_images(staging, count=2)

        loaded = LoadedInput(
            kind="epub", input_path=tmpdir / "in.epub", work_dir=staging,
            rel_names=["page_001.png", "page_002.png"], pages=orig_imgs,
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())
        (staging / "mimetype").write_text("application/epub+zip")

        output_epub = tmpdir / "output.epub"
        result = pack_output(loaded, trans_dir, output_epub)  # 不应抛异常

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" in names


# ---------------------------------------------------------------- 按 dpi 降采样超大图片


def test_target_long_edge_px_at_150dpi():
    # B5 参考开本长边 10.1 英寸 × 150dpi = 1515px（四舍五入）
    assert target_long_edge_px(150) == 1515


def test_target_long_edge_px_at_300dpi():
    assert target_long_edge_px(300) == 3030


def test_downscaled_size_returns_none_when_within_target():
    """已经不超过目标长边的图不处理——不放大小图。"""
    assert downscaled_size(800, 1200, target_long_edge=1515) is None
    assert downscaled_size(1200, 1515, target_long_edge=1515) is None  # 恰好等于也不处理


def test_downscaled_size_shrinks_oversized_preserving_aspect_ratio():
    new_w, new_h = downscaled_size(4535, 6307, target_long_edge=1515)
    assert new_h == 1515  # 长边（这里是高）缩到目标
    # 宽高比误差控制在四舍五入范围内
    assert abs(new_w / new_h - 4535 / 6307) < 0.001


def test_load_input_folder_downscales_oversized_image(tmp_path):
    """真实踩过的坑（2026-08-22）：一本同人志原图长边 6307px，LaMa 抹字实测约
    4.4 分钟/页。load_input 对图片输入按 cfg.pdf_dpi 换算的参考开本长边做降采样，
    默认 150dpi 下超过 1515px 长边的图会被等比缩小。
    """
    src_dir = tmp_path / "book"
    src_dir.mkdir()
    big = Image.new("RGB", (4535, 6307), color=(255, 255, 255))
    big.save(src_dir / "01.jpg", quality=95)

    loaded = load_input(src_dir, tmp_path / "_extract")

    assert len(loaded.pages) == 1
    with Image.open(loaded.pages[0]) as im:
        assert max(im.size) <= 1515
        assert abs(im.size[0] / im.size[1] - 4535 / 6307) < 0.001
    # 原图必须原样保留，不能被就地覆盖——回退路径/用户原始数据不该被这一步改动。
    with Image.open(src_dir / "01.jpg") as orig:
        assert orig.size == (4535, 6307)


def test_load_input_folder_leaves_normal_size_images_untouched(tmp_path):
    """没超过目标长边的图不应该被复制/重新编码——直接复用原路径，省 IO。"""
    src_dir = tmp_path / "book"
    src_dir.mkdir()
    small = Image.new("RGB", (800, 1200), color=(255, 255, 255))
    small.save(src_dir / "01.png")

    loaded = load_input(src_dir, tmp_path / "_extract")

    assert loaded.pages == [src_dir / "01.png"]  # 原路径，不是复制/缩放后的路径


def test_load_input_folder_downscale_avoids_filename_collision_across_subfolders(tmp_path):
    """真实场景：漫画按分卷子文件夹组织，不同子卷可能撞同名文件（"01.jpg"）。
    降采样落盘必须按相对路径建目录结构，不能只用 basename，否则会互相覆盖。
    """
    src_dir = tmp_path / "book"
    (src_dir / "vol1").mkdir(parents=True)
    (src_dir / "vol2").mkdir(parents=True)
    Image.new("RGB", (4535, 6307), color=(255, 0, 0)).save(src_dir / "vol1" / "01.jpg", quality=95)
    Image.new("RGB", (4535, 6307), color=(0, 0, 255)).save(src_dir / "vol2" / "01.jpg", quality=95)

    loaded = load_input(src_dir, tmp_path / "_extract")

    assert len(loaded.pages) == 2
    assert len({p.resolve() for p in loaded.pages}) == 2  # 两个不同的落盘路径，没有互相覆盖
    # JPEG 有损压缩会让像素值有 ±1~2 的漂移（quality=95 下实测确认过），不能按精确值
    # 比对；只验证"哪张红哪张蓝"没有串——真正要防的是文件名碰撞导致互相覆盖。
    colors = [Image.open(p).convert("RGB").getpixel((0, 0)) for p in loaded.pages]
    reds = [c for c in colors if c[0] > 200 and c[2] < 50]
    blues = [c for c in colors if c[2] > 200 and c[0] < 50]
    assert len(reds) == 1 and len(blues) == 1


def test_load_input_pdf_is_not_downscaled_again(tmp_path, monkeypatch):
    """PDF 已经按 dpi 从矢量渲染，物理尺寸精确已知，不该再套图片输入的那层
    "参考开本估算"降采样——用一个假的 _load_pdf 验证 load_input 对 pdf 分支直接
    return，不会把结果再传进 _downscale_oversized_images。
    """
    import comic_gen.pipeline.loader as loader_mod

    calls = []
    fake_loaded = LoadedInput(
        kind="pdf", input_path=tmp_path / "in.pdf", work_dir=tmp_path,
        rel_names=["page_0001.png"], pages=[tmp_path / "page_0001.png"],
    )

    def fake_load_pdf(p, out, dpi, load_cb):
        calls.append(dpi)
        return fake_loaded

    monkeypatch.setattr(loader_mod, "_load_pdf", fake_load_pdf)
    pdf_path = tmp_path / "in.pdf"
    pdf_path.write_bytes(b"fake")

    result = load_input(pdf_path, tmp_path / "_extract")

    assert result is fake_loaded  # 原样返回，没有被 _downscale_oversized_images 处理过
    assert calls == [150]
