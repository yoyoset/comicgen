"""生成一个最小合法 EPUB 样本（2 页漫画），用于测试 EPUB 输入输出。"""
import shutil
import zipfile
from pathlib import Path

MIMETYPE = "application/epub+zip"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">sample-epub</dc:identifier><dc:title>sample</dc:title><dc:language>ja</dc:language>
  </metadata>
  <manifest>
    <item id="p1" href="p1.xhtml" media-type="application/xhtml+xml"/>
    <item id="p2" href="p2.xhtml" media-type="application/xhtml+xml"/>
    <item id="i1" href="images/page_001.png" media-type="image/png"/>
    <item id="i2" href="images/page_002.png" media-type="image/png"/>
  </manifest>
  <spine><itemref idref="p1"/><itemref idref="p2"/></spine>
</package>
"""

XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{n}</title></head>
<body><div><img src="images/page_00{n}.png" alt="{n}"/></div></body></html>
"""


def make_epub(img1: str, img2: str, out: str = r"tests\sample.epub") -> str:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", OPF, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/p1.xhtml", XHTML.format(n=1), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/p2.xhtml", XHTML.format(n=2), compress_type=zipfile.ZIP_DEFLATED)
        zf.write(img1, "OEBPS/images/page_001.png", compress_type=zipfile.ZIP_DEFLATED)
        zf.write(img2, "OEBPS/images/page_002.png", compress_type=zipfile.ZIP_DEFLATED)
    return out


if __name__ == "__main__":
    src = Path(r"tests\sample\page_001.png")
    img2 = Path(r"tests\sample\page_002.png")
    if not img2.exists():
        shutil.copy2(src, img2)
    print(make_epub(str(src), str(img2)))
