#!/usr/bin/env python3
"""Tests for compress.py. Run with: python3 test_compress.py"""

from __future__ import annotations

import io
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compress  # noqa: E402

TARGET = 150 * 1024


def noisy_image(w: int, h: int, seed: int = 0) -> Image.Image:
    """Deliberately hard to compress, so the search has to work for it."""
    random.seed(seed)
    base = Image.new("RGB", (200, 150))
    draw = ImageDraw.Draw(base)
    for _ in range(300):
        x0, x1 = sorted(random.sample(range(200), 2))
        y0, y1 = sorted(random.sample(range(150), 2))
        draw.ellipse([x0, y0, x1, y1],
                     fill=tuple(random.randint(0, 255) for _ in range(3)))
    big = base.resize((w, h), Image.BICUBIC)
    noise = Image.effect_noise((w, h), 70).convert("L")
    return Image.merge("RGB", [Image.blend(c, noise, 0.3) for c in big.split()])


def text_pdf(path: Path, pages: int = 4) -> None:
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_text((60, 80), f"Section {n + 1}", fontsize=20)
        for line in range(40):
            page.insert_text((60, 120 + line * 16),
                             "The quick brown fox jumps over the lazy dog. " * 2,
                             fontsize=10)
    doc.save(path)
    doc.close()


def scan_pdf(path: Path, pages: int = 6, seed: int = 1, distinct: bool = False) -> None:
    """A scan-like PDF. `distinct` gives every page its own image, which stops
    PDF object de-duplication from making a many-page file trivially small."""
    doc = pymupdf.open()
    shared = None
    for n in range(pages):
        if distinct:
            buf = io.BytesIO()
            noisy_image(1100, 850, seed + n).save(buf, format="JPEG", quality=95)
            data = buf.getvalue()
        else:
            if shared is None:
                buf = io.BytesIO()
                noisy_image(1600, 1200, seed).save(buf, format="JPEG", quality=95)
                shared = buf.getvalue()
            data = shared
        page = doc.new_page()
        page.insert_image(page.rect, stream=data)
    doc.save(path)
    doc.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.out = self.dir / "out"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cli(self, *args) -> int:
        return compress.main([*args, "-o", str(self.out), "-q"])

    def outputs(self) -> list[Path]:
        return sorted(self.out.glob("*.pdf"))

    def assert_all_within(self, budget: int = TARGET):
        produced = self.outputs()
        self.assertTrue(produced, "no output produced")
        for pdf in produced:
            size = pdf.stat().st_size
            self.assertLessEqual(
                size, budget,
                f"{pdf.name} is {size} bytes, over the {budget} byte budget")
            with pymupdf.open(pdf) as doc:  # must be a readable PDF
                self.assertGreater(doc.page_count, 0)


class TestSizes(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(compress.parse_size("150kb"), 150 * 1024)
        self.assertEqual(compress.parse_size("150 KB"), 150 * 1024)
        self.assertEqual(compress.parse_size("1.5mb"), int(1.5 * 1024 ** 2))
        self.assertEqual(compress.parse_size("2048"), 2048)
        self.assertEqual(compress.parse_size("1MiB"), 1024 ** 2)
        for bad in ("", "abc", "-5kb", "0"):
            with self.assertRaises(Exception):
                compress.parse_size(bad)

    def test_ladder_search_finds_leftmost_fitting(self):
        ladder = (100, 80, 60, 40, 20)  # stand-in "sizes"
        seen = []

        def build(n):
            seen.append(n)
            return b"x" * n

        setting, data = compress.ladder_search(build, ladder, 60)
        self.assertEqual(setting, 60, "should pick the best quality that fits")
        self.assertEqual(len(data), 60)
        self.assertLess(len(seen), len(ladder), "should not scan the whole ladder")

    def test_ladder_search_returns_none_when_nothing_fits(self):
        self.assertIsNone(
            compress.ladder_search(lambda n: b"x" * n, (100, 80, 60), 10))


class TestImages(Base):
    def test_large_photo_fits_budget(self):
        noisy_image(3000, 2200).save(self.dir / "photo.jpg", quality=96)
        self.assertEqual(self.run_cli(str(self.dir / "photo.jpg")), 0)
        self.assert_all_within()

    def test_page_keeps_original_aspect_ratio(self):
        noisy_image(2000, 1000).save(self.dir / "wide.jpg", quality=95)
        self.run_cli(str(self.dir / "wide.jpg"))
        with pymupdf.open(self.outputs()[0]) as doc:
            rect = doc[0].rect
            self.assertAlmostEqual(rect.width / rect.height, 2.0, places=2)

    def test_several_formats(self):
        img = noisy_image(1200, 900)
        for name in ("a.png", "b.bmp", "c.tiff", "d.webp"):
            img.save(self.dir / name)
        self.assertEqual(self.run_cli(str(self.dir)), 0)
        self.assertEqual(len(self.outputs()), 4)
        self.assert_all_within()

    def test_jpeg_that_fits_is_embedded_without_re_encoding(self):
        """Re-encoding a JPEG that already fits only adds loss and bytes."""
        import hashlib
        src = self.dir / "small.jpg"
        noisy_image(600, 400).save(src, quality=70)
        self.assertLess(src.stat().st_size, TARGET, "fixture must fit the budget")
        self.run_cli(str(src))
        out = self.outputs()[0]
        self.assertLess(out.stat().st_size, src.stat().st_size + 20 * 1024)
        with pymupdf.open(out) as doc:
            xref = doc[0].get_images(full=True)[0][0]
            embedded = doc.extract_image(xref)["image"]
        self.assertEqual(hashlib.md5(embedded).hexdigest(),
                         hashlib.md5(src.read_bytes()).hexdigest(),
                         "the JPEG was re-encoded instead of embedded as-is")

    def test_rotated_jpeg_is_re_encoded_not_passed_through(self):
        """EXIF rotation has to be baked in, so passthrough must be skipped."""
        src = self.dir / "rot.jpg"
        img = noisy_image(800, 600)
        exif = img.getexif()
        exif[274] = 6  # rotate 90 CW
        img.save(src, quality=80, exif=exif)
        self.run_cli(str(src))
        with pymupdf.open(self.outputs()[0]) as doc:
            rect = doc[0].rect
        self.assertGreater(rect.height, rect.width,
                           "rotation was not applied to the page")

    def test_tiny_image_is_not_inflated(self):
        Image.new("RGB", (48, 48), "teal").save(self.dir / "t.png")
        self.run_cli(str(self.dir / "t.png"))
        self.assertLess(self.outputs()[0].stat().st_size, 20 * 1024)

    def test_smaller_budget_is_respected(self):
        noisy_image(2600, 2000).save(self.dir / "p.jpg", quality=96)
        self.assertEqual(self.run_cli(str(self.dir / "p.jpg"), "-t", "40kb"), 0)
        self.assert_all_within(40 * 1024)


class TestPdfs(Base):
    def test_small_text_pdf_passes_through_with_text_intact(self):
        text_pdf(self.dir / "doc.pdf")
        self.assertEqual(self.run_cli(str(self.dir / "doc.pdf")), 0)
        self.assert_all_within()
        with pymupdf.open(self.outputs()[0]) as doc:
            self.assertGreater(len(doc[0].get_text().strip()), 100)

    def test_heavy_scan_is_compressed(self):
        scan_pdf(self.dir / "scan.pdf")
        before = (self.dir / "scan.pdf").stat().st_size
        self.assertEqual(self.run_cli(str(self.dir / "scan.pdf")), 0)
        self.assert_all_within()
        self.assertLess(self.outputs()[0].stat().st_size, before)

    def test_text_layer_survives_when_images_are_shrunk(self):
        doc = pymupdf.open()
        buf = io.BytesIO()
        noisy_image(1500, 1100).save(buf, format="JPEG", quality=94)
        for n in range(10):
            page = doc.new_page()
            page.insert_image(pymupdf.Rect(40, 40, 555, 430), stream=buf.getvalue())
            page.insert_text((60, 500), f"Invoice {n + 1} payable on receipt, "
                                        "reference ABC-12345, total 199.00", fontsize=11)
        doc.save(self.dir / "mixed.pdf")
        doc.close()
        self.assertEqual(self.run_cli(str(self.dir / "mixed.pdf")), 0)
        self.assert_all_within()
        with pymupdf.open(self.outputs()[0]) as out:
            self.assertIn("ABC-12345", out[0].get_text())

    def test_page_count_is_preserved(self):
        scan_pdf(self.dir / "s.pdf", pages=7)
        self.run_cli(str(self.dir / "s.pdf"))
        with pymupdf.open(self.outputs()[0]) as doc:
            self.assertEqual(doc.page_count, 7)

    def test_greyscale_option(self):
        scan_pdf(self.dir / "s.pdf")
        self.assertEqual(self.run_cli(str(self.dir / "s.pdf"), "--gray"), 0)
        self.assert_all_within()

    def test_budget_is_used_rather_than_undershot(self):
        """A generous budget must not produce a needlessly tiny, blurry file."""
        scan_pdf(self.dir / "s.pdf", pages=8)
        budget = 400 * 1024
        self.assertEqual(self.run_cli(str(self.dir / "s.pdf"), "-t", "400kb"), 0)
        size = self.outputs()[0].stat().st_size
        self.assert_all_within(budget)
        self.assertGreater(size, budget * 0.4,
                           "compressed far below the budget; quality was wasted")


class TestStages(unittest.TestCase):
    """Guards for stages that fail silently and fall through to a worse one."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        scan_pdf(self.dir / "s.pdf")
        self.src = (self.dir / "s.pdf").read_bytes()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_rewrite_images_stage_actually_runs(self):
        out = compress._rewrite_images(self.src, 72, 40, False)
        self.assertIsNotNone(out, "image re-encode stage returned nothing")
        self.assertLess(len(out), len(self.src))

    def test_rasterise_stage_actually_runs(self):
        out = compress._rasterise(self.src, 800, 40, False)
        self.assertIsNotNone(out, "rasterise stage returned nothing")
        self.assertLess(len(out), len(self.src))

    def test_rasterise_sizes_pages_in_pixels_not_dpi(self):
        """Pages with unequal page boxes must still land at equal resolution."""
        doc = pymupdf.open()
        buf = io.BytesIO()
        noisy_image(1200, 1600, 4).save(buf, format="JPEG", quality=92)
        for width, height in ((595, 842), (1700, 2400)):  # A4, then a huge box
            page = doc.new_page(width=width, height=height)
            page.insert_image(page.rect, stream=buf.getvalue())
        raw = doc.tobytes()
        doc.close()

        out = compress._rasterise(raw, 900, 50, False)
        with pymupdf.open(stream=out, filetype="pdf") as result:
            sizes = [max(img[2], img[3])
                     for page in result for img in page.get_images(full=True)]
        self.assertEqual(len(sizes), 2)
        self.assertTrue(all(abs(s - 900) <= 2 for s in sizes),
                        f"pages rendered at unequal resolutions: {sizes}")

    def test_lossless_stage_keeps_pages(self):
        out = compress._lossless(self.src)
        with pymupdf.open(stream=out, filetype="pdf") as doc:
            self.assertEqual(doc.page_count, 6)

    def test_has_text_detection(self):
        text_pdf(self.dir / "t.pdf")
        self.assertTrue(compress._has_text((self.dir / "t.pdf").read_bytes()))
        self.assertFalse(compress._has_text(self.src))


class TestSplitting(Base):
    def test_split_parts_each_fit_the_budget(self):
        scan_pdf(self.dir / "big.pdf", pages=10, distinct=True)
        budget = 20 * 1024
        code = self.run_cli(str(self.dir / "big.pdf"), "-t", "20kb", "--split")
        self.assertEqual(code, 0)
        produced = self.outputs()
        self.assertGreater(len(produced), 1, "expected the document to be split")
        self.assert_all_within(budget)
        total = sum(pymupdf.open(p).page_count for p in produced)
        with pymupdf.open(self.dir / "big.pdf") as src:
            self.assertEqual(total, src.page_count, "pages lost while splitting")

    def test_without_split_an_impossible_target_reports_failure(self):
        scan_pdf(self.dir / "big.pdf", pages=10, distinct=True)
        self.assertEqual(self.run_cli(str(self.dir / "big.pdf"), "-t", "3kb"), 1)


class TestBatch(Base):
    def test_mixed_batch_and_recursion(self):
        nested = self.dir / "sub" / "deep"
        nested.mkdir(parents=True)
        noisy_image(1400, 1000).save(self.dir / "a.jpg", quality=95)
        noisy_image(1400, 1000, 2).save(nested / "b.png")
        text_pdf(self.dir / "sub" / "c.pdf")
        self.assertEqual(self.run_cli(str(self.dir), "-r"), 0)
        self.assertEqual({p.stem for p in self.outputs()}, {"a", "b", "c"})
        self.assert_all_within()

    def test_name_collision_does_not_overwrite(self):
        noisy_image(900, 700).save(self.dir / "same.jpg", quality=95)
        text_pdf(self.dir / "same.pdf")
        self.assertEqual(self.run_cli(str(self.dir)), 0)
        self.assertEqual(len(self.outputs()), 2, "one output overwrote the other")

    def test_in_place_run_never_clobbers_the_source(self):
        text_pdf(self.dir / "doc.pdf")
        before = (self.dir / "doc.pdf").read_bytes()
        compress.main([str(self.dir / "doc.pdf"), "-o", str(self.dir), "-q"])
        self.assertEqual((self.dir / "doc.pdf").read_bytes(), before,
                         "the input file was overwritten")

    def test_rerun_overwrites_instead_of_accumulating(self):
        noisy_image(900, 700).save(self.dir / "a.jpg", quality=95)
        for _ in range(3):
            self.run_cli(str(self.dir / "a.jpg"))
        self.assertEqual(len(self.outputs()), 1)

    def test_unreadable_file_is_reported_not_fatal(self):
        (self.dir / "broken.pdf").write_bytes(b"this is not a pdf at all")
        noisy_image(900, 700).save(self.dir / "fine.jpg", quality=95)
        self.assertEqual(self.run_cli(str(self.dir)), 1)  # non-zero: one failed
        self.assertEqual([p.stem for p in self.outputs()], ["fine"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
