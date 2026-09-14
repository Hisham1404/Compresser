#!/usr/bin/env python3
"""Compress PDFs and images into individual PDFs that fit a hard size budget.

Every input file becomes its own output PDF, at most --target bytes (150 KB by
default). Quality is pushed as high as the budget allows rather than being
fixed up front: each stage searches a ladder of settings and keeps the best
result that still fits.

    python3 compress.py scans/ photo.jpg -o out/
    python3 compress.py big.pdf --target 100kb --gray --split
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover - import guard
    sys.exit("PyMuPDF is required: pip install -r requirements.txt")

from PIL import Image, ImageOps

try:  # optional: HEIC/HEIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

Image.MAX_IMAGE_PIXELS = None  # large scans are expected, not an attack

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".ppm", ".pgm", ".jp2", ".heic", ".heif",
}

DEFAULT_TARGET = 150 * 1024

DEBUG = False  # --debug: report errors that are otherwise swallowed per file

# Downscale ladder for images: fraction of the original pixel dimensions.
IMAGE_SCALES = (1.0, 0.85, 0.72, 0.6, 0.5, 0.42, 0.35, 0.28, 0.22, 0.18, 0.14, 0.1, 0.07)
# (dpi, jpeg quality) ladder shared by the PDF re-encode and rasterise stages,
# best quality first so that output size falls as the index grows.
PDF_LADDER = (
    (300, 85), (260, 82), (240, 80), (220, 76), (200, 72), (185, 70),
    (170, 68), (160, 64), (150, 62), (140, 60), (130, 56), (120, 54),
    (110, 50), (100, 48), (96, 45), (88, 42), (80, 40), (72, 38),
    (66, 35), (60, 32), (54, 30), (50, 27), (46, 25), (42, 22),
    (38, 20), (34, 18), (30, 15), (26, 12), (22, 10),
)
# Rasterising ladder: (long edge in pixels, jpeg quality). Pages are sized in
# pixels rather than by DPI because a page box says nothing reliable about how
# big the page really is -- a scan wrapped at one pixel per point produces a
# box several times larger than the A4 page beside it, and a uniform DPI would
# hand that page most of the budget while starving the rest.
RASTER_LADDER = (
    (2600, 85), (2400, 82), (2200, 80), (2000, 76), (1900, 74), (1800, 72),
    (1700, 70), (1600, 68), (1500, 64), (1400, 62), (1300, 60), (1200, 56),
    (1100, 54), (1000, 50), (950, 48), (900, 45), (850, 42), (800, 40),
    (750, 38), (700, 35), (650, 32), (600, 30), (550, 27), (500, 25),
    (450, 22), (400, 20), (350, 18), (300, 15), (260, 12), (220, 10),
)
# Quality steps used when spending leftover budget at a fixed resolution.
QUALITY_STEPS = (95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def parse_size(text: str) -> int:
    """Turn '150kb', '1.5 MB', '204800' into a byte count."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmg]?i?b?)\s*", str(text), re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(f"cannot read size: {text!r}")
    value, unit = float(m.group(1)), m.group(2).lower().rstrip("b").rstrip("i")
    factor = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit]
    size = int(value * factor)
    if size <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return size


def human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.2f} MB"


def ladder_search(build, ladder, budget):
    """Return (setting, data) for the best-quality ladder entry that fits.

    `ladder` runs best quality first, so output size decreases as the index
    grows; a binary search finds the leftmost entry within budget. Returns
    None when even the last entry is too big.
    """
    lo, hi, best = 0, len(ladder) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        data = build(ladder[mid])
        if data is not None and len(data) <= budget:
            best = (ladder[mid], data)
            hi = mid - 1
        else:
            lo = mid + 1
    return best


# --------------------------------------------------------------------------- #
# images -> pdf
# --------------------------------------------------------------------------- #

def _jpeg_bytes(img: Image.Image, scale: float, quality: int) -> bytes:
    w = max(1, int(round(img.width * scale)))
    h = max(1, int(round(img.height * scale)))
    frame = img if (w, h) == img.size else img.resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    frame.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _jpeg_to_pdf(jpeg: bytes, page_w: float, page_h: float) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_image(pymupdf.Rect(0, 0, page_w, page_h), stream=jpeg)
    out = doc.tobytes(garbage=4, deflate=True, use_objstms=1)
    doc.close()
    return out


def compress_image(path: Path, opt: "Options") -> "Result":
    with Image.open(path) as raw:
        source_format = raw.format
        source_mode = raw.mode
        # 274 is the EXIF orientation tag; anything but 1 needs the rotation
        # baked in, which means re-encoding.
        upright = (raw.getexif().get(274) or 1) == 1
        img = ImageOps.exif_transpose(raw)
        img = img.convert("L" if opt.gray else "RGB")
        orig_w, orig_h = img.width, img.height

        # Page geometry comes from the *original* pixel size, so downscaling
        # changes the file size without shrinking the printed page.
        page_w = orig_w / opt.page_dpi * 72.0
        page_h = orig_h / opt.page_dpi * 72.0
        if opt.page_fit == "a4":
            box_w, box_h = (595.0, 842.0) if orig_h >= orig_w else (842.0, 595.0)
            ratio = min(box_w / page_w, box_h / page_h, 1.0)
            page_w, page_h = page_w * ratio, page_h * ratio

        # A JPEG that already fits can be embedded byte for byte. Re-encoding
        # it would add a second round of JPEG loss and usually make it larger,
        # so this is better on both size and quality when it is available.
        if (source_format == "JPEG" and upright and not opt.gray
                and source_mode in ("RGB", "L")):
            direct = _jpeg_to_pdf(path.read_bytes(), page_w, page_h)
            if len(direct) <= opt.target:
                return Result(path, direct,
                              f"jpeg embedded unchanged ({orig_w}x{orig_h})")

        best = None  # (scale, quality, pdf_bytes)
        for scale in IMAGE_SCALES:
            hit = ladder_search(
                lambda q, s=scale: _jpeg_to_pdf(_jpeg_bytes(img, s, q), page_w, page_h),
                tuple(range(95, opt.min_quality - 1, -5)),
                opt.target,
            )
            if hit is None:
                continue
            quality, data = hit
            if best is None or quality > best[1]:
                best = (scale, quality, data)
            # Highest scale that still holds a comfortable quality wins.
            if quality >= opt.quality_floor:
                break

        if best is None:  # nothing fit; emit the smallest thing we can make
            floor_scale, floor_q = IMAGE_SCALES[-1], opt.min_quality
            data = _jpeg_to_pdf(_jpeg_bytes(img, floor_scale, floor_q), page_w, page_h)
            return Result(path, data, f"image jpeg q{floor_q} @{floor_scale:.0%}", over=True)

        scale, quality, data = best
        note = f"image jpeg q{quality} @{scale:.0%} ({int(orig_w * scale)}x{int(orig_h * scale)})"
        return Result(path, data, note)


# --------------------------------------------------------------------------- #
# pdf -> pdf
# --------------------------------------------------------------------------- #

def _save_bytes(doc: pymupdf.Document) -> bytes:
    return doc.tobytes(
        garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
        clean=True, use_objstms=1,
    )


def _lossless(src: bytes) -> bytes:
    """Structural cleanup only: fonts subset, streams packed, junk dropped."""
    doc = pymupdf.open(stream=src, filetype="pdf")
    try:
        doc.subset_fonts()
    except Exception as exc:
        _note_failure("subset_fonts", exc)  # a bad font table must not sink the file
    out = _save_bytes(doc)
    doc.close()
    return out


def _note_failure(stage: str, exc: Exception) -> None:
    if DEBUG:
        print(f"    debug: {stage} failed -- {type(exc).__name__}: {exc}", file=sys.stderr)


def _rewrite_images(src: bytes, dpi: int, quality: int, gray: bool) -> bytes | None:
    """Re-encode embedded images, leaving text and vectors selectable."""
    doc = pymupdf.open(stream=src, filetype="pdf")
    try:
        # Only images above the threshold are touched, and the threshold must
        # sit strictly above the target resolution.
        doc.rewrite_images(
            dpi_threshold=dpi + 1, dpi_target=dpi, quality=quality,
            lossy=True, lossless=True, bitonal=False, set_to_gray=gray,
        )
        out = _save_bytes(doc)
    except Exception as exc:
        _note_failure(f"rewrite_images {dpi}dpi q{quality}", exc)
        out = None
    finally:
        doc.close()
    return out


def _rasterise(src: bytes, long_edge: int, quality: int, gray: bool,
               pages: list[int] | None = None) -> bytes | None:
    """Last resort: every page becomes one JPEG. Text stops being selectable.

    Each page is rendered so its longer side is `long_edge` pixels, which keeps
    every page at a comparable resolution however its page box is defined.
    """
    doc = pymupdf.open(stream=src, filetype="pdf")
    out_doc = pymupdf.open()
    cs = pymupdf.csGRAY if gray else pymupdf.csRGB
    try:
        for number in (pages if pages is not None else range(doc.page_count)):
            page = doc[number]
            rect = page.rect
            zoom = long_edge / max(rect.width, rect.height, 1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                  colorspace=cs, annots=True)
            jpeg = pix.tobytes("jpeg", jpg_quality=quality)
            new = out_doc.new_page(width=rect.width, height=rect.height)
            new.insert_image(new.rect, stream=jpeg)
        data = out_doc.tobytes(garbage=4, deflate=True, use_objstms=1)
    except Exception as exc:
        _note_failure(f"rasterise {long_edge}px q{quality}", exc)
        data = None
    finally:
        out_doc.close()
        doc.close()
    return data


def _has_text(src: bytes, sample: int = 8) -> bool:
    """True when the document carries a real text layer worth protecting."""
    doc = pymupdf.open(stream=src, filetype="pdf")
    try:
        for number in range(min(sample, doc.page_count)):
            if len(doc[number].get_text().strip()) > 40:
                return True
    except Exception as exc:
        _note_failure("text probe", exc)
    finally:
        doc.close()
    return False


def _refine_quality(build, size: int, base_quality: int, budget: int):
    """Spend whatever budget is left on quality, keeping the resolution.

    `size` is whichever resolution figure the ladder in use carries -- DPI when
    re-encoding embedded images, pixels on the long edge when rasterising.
    """
    steps = tuple(q for q in QUALITY_STEPS if q > base_quality)
    if not steps:
        return None
    return ladder_search(lambda q: build(size, q), steps, budget)


def _best_fit(build, budget: int, ladder=PDF_LADDER):
    """Best ladder rung that fits, with leftover budget spent on quality."""
    hit = ladder_search(lambda s: build(s[0], s[1]), ladder, budget)
    if hit is None:
        return None
    (size, quality), data = hit
    better = _refine_quality(build, size, quality, budget)
    if better and len(better[1]) > len(data):
        quality, data = better
    return (size, quality), data


def compress_pdf(path: Path, opt: "Options") -> "Result":
    src = path.read_bytes()
    budget = opt.target

    # Check it really is a readable PDF, so a damaged file is reported rather
    # than copied through untouched by the under-budget shortcut below.
    with pymupdf.open(stream=src, filetype="pdf") as probe:
        if probe.needs_pass:
            raise ValueError("PDF is password protected")
        if probe.page_count == 0:
            raise ValueError("PDF has no pages")

    if not opt.gray and len(src) <= budget:
        return Result(path, src, "already under budget (copied)")

    cleaned = _lossless(src)
    if len(cleaned) <= budget and not opt.gray:
        return Result(path, cleaned, "lossless cleanup")
    working = cleaned if len(cleaned) < len(src) else src

    # Re-encoding the embedded images keeps text and vectors selectable, but
    # PyMuPDF only resamples at coarse resolution steps, so the result can sit
    # well under the budget. Rasterising tracks the budget closely and is the
    # better choice when there is no text layer to lose.
    rewritten = _best_fit(
        lambda dpi, q: _rewrite_images(working, dpi, q, opt.gray), budget
    )
    if rewritten and _has_text(working):
        (dpi, quality), data = rewritten
        return Result(path, data, f"images re-encoded {dpi}dpi q{quality}")

    rasterised = _best_fit(
        lambda px, q: _rasterise(working, px, q, opt.gray), budget, RASTER_LADDER
    )
    if rewritten and rasterised:
        # Neither keeps a text layer here, so prefer the one that uses more of
        # the budget -- that is the one carrying more detail.
        if len(rewritten[1]) >= len(rasterised[1]):
            (dpi, quality), data = rewritten
            return Result(path, data, f"images re-encoded {dpi}dpi q{quality}")
    if rasterised:
        (px, quality), data = rasterised
        return Result(path, data, f"rasterised {px}px q{quality}")
    if rewritten:
        (dpi, quality), data = rewritten
        return Result(path, data, f"images re-encoded {dpi}dpi q{quality}")

    # The floor is still too big -- usually a long document.
    px, quality = RASTER_LADDER[-1]
    note = f"rasterised {px}px q{quality} (floor)"
    if opt.split:
        parts = _split(working, opt)
        if len(parts) > 1:
            return Result(path, parts[0], f"split into {len(parts)} parts",
                          extra_parts=parts[1:],
                          over=any(len(p) > budget for p in parts))
    floor = _rasterise(working, px, quality, opt.gray) or working
    return Result(path, floor, note, over=len(floor) > budget)


def _chunk_end(src: bytes, start: int, page_count: int, opt: "Options") -> int:
    """Largest page after `start` that still fits, measured at floor quality."""
    px, quality = RASTER_LADDER[-1]
    end = page_count
    while end > start + 1:
        data = _rasterise(src, px, quality, opt.gray, pages=list(range(start, end)))
        if data is not None and len(data) <= opt.target:
            return end
        end = start + max(1, (end - start) // 2)
    return start + 1  # a single page, whether or not it fits


def _split(src: bytes, opt: "Options") -> list[bytes]:
    """Cut the document into parts that each fit the budget.

    Each part is sized at floor quality so that it is known to be feasible,
    then re-encoded at the best quality that part's headroom allows.
    """
    doc = pymupdf.open(stream=src, filetype="pdf")
    page_count = doc.page_count
    doc.close()

    parts: list[bytes] = []
    start = 0
    while start < page_count:
        end = _chunk_end(src, start, page_count, opt)
        pages = list(range(start, end))
        hit = _best_fit(
            lambda px, q: _rasterise(src, px, q, opt.gray, pages), opt.target,
            RASTER_LADDER
        )
        if hit:
            parts.append(hit[1])
        else:  # single page that will not fit even at the floor
            px, quality = RASTER_LADDER[-1]
            parts.append(_rasterise(src, px, quality, opt.gray, pages) or b"")
        start = end
    return parts


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #

@dataclass
class Options:
    target: int = DEFAULT_TARGET
    gray: bool = False
    page_dpi: int = 150
    page_fit: str = "auto"
    min_quality: int = 20
    quality_floor: int = 60
    split: bool = False


@dataclass
class Result:
    source: Path
    data: bytes
    method: str
    over: bool = False
    extra_parts: list[bytes] = field(default_factory=list)
    error: str | None = None
    written: list[Path] = field(default_factory=list)


def collect_inputs(paths: list[str], recursive: bool) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            walk = p.rglob("*") if recursive else p.glob("*")
            found += sorted(f for f in walk
                            if f.is_file() and f.suffix.lower() in PDF_EXTS | IMAGE_EXTS)
        elif p.is_file():
            found.append(p)
        else:
            print(f"! skipping {p}: not found", file=sys.stderr)
    seen, unique = set(), []
    for f in found:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def unique_path(out_dir: Path, stem: str, taken: set) -> Path:
    candidate = out_dir / f"{stem}.pdf"
    n = 1
    while candidate.resolve() in taken:
        candidate = out_dir / f"{stem}-{n}.pdf"
        n += 1
    taken.add(candidate.resolve())
    return candidate


def process(path: Path, opt: Options) -> Result:
    try:
        if path.suffix.lower() in PDF_EXTS:
            return compress_pdf(path, opt)
        return compress_image(path, opt)
    except Exception as exc:  # one bad file must not stop the batch
        return Result(path, b"", "", error=f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Compress PDFs and images into separate PDFs under a size budget.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+", help="files and/or directories")
    ap.add_argument("-o", "--out", default="compressed", help="output directory")
    ap.add_argument("-t", "--target", type=parse_size, default=DEFAULT_TARGET,
                    metavar="SIZE", help="max size per output PDF, e.g. 150kb")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="descend into sub-directories")
    ap.add_argument("--gray", action="store_true",
                    help="convert to greyscale (much smaller for scans and text)")
    ap.add_argument("--split", action="store_true",
                    help="split a PDF that cannot fit into numbered parts")
    ap.add_argument("--page-dpi", type=int, default=150,
                    help="assumed DPI when turning an image into a page")
    ap.add_argument("--page-fit", choices=("auto", "a4"), default="auto",
                    help="'auto' matches the image aspect, 'a4' fits onto A4")
    ap.add_argument("--min-quality", type=int, default=20,
                    help="lowest JPEG quality the search may use")
    ap.add_argument("--quality-floor", type=int, default=60,
                    help="quality considered good enough to stop downscaling")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, (os.cpu_count() or 2)),
                    help="files to process in parallel")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print problems")
    ap.add_argument("--debug", action="store_true",
                    help="report errors that are otherwise handled silently")
    args = ap.parse_args(argv)

    global DEBUG
    DEBUG = args.debug

    opt = Options(
        target=args.target, gray=args.gray, page_dpi=args.page_dpi,
        page_fit=args.page_fit, min_quality=args.min_quality,
        quality_floor=args.quality_floor, split=args.split,
    )

    files = collect_inputs(args.inputs, args.recursive)
    if not files:
        print("no PDFs or images found", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = {f.resolve() for f in files}

    if not args.quiet:
        print(f"{len(files)} file(s) -> {out_dir}/  budget {human(opt.target)}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(pool.map(lambda f: process(f, opt), files))

    taken: set = set()
    failures = 0
    for src, res in zip(files, results):
        if res.error:
            failures += 1
            print(f"  FAIL  {src.name}: {res.error}", file=sys.stderr)
            continue

        blobs = [res.data] + res.extra_parts
        for index, blob in enumerate(blobs):
            stem = src.stem if len(blobs) == 1 else f"{src.stem}-part{index + 1}"
            dest = unique_path(out_dir, stem, taken)
            if dest.resolve() in sources:  # never clobber an input
                dest = unique_path(out_dir, f"{stem}-compressed", taken)
            dest.write_bytes(blob)
            res.written.append(dest)

        total = sum(len(b) for b in blobs)
        before = src.stat().st_size
        flag = "OVER" if res.over else "ok"
        if res.over:
            failures += 1
        if not args.quiet or res.over:
            if len(res.written) > 3:  # keep long split runs to one line
                names = (f"{res.written[0].name} .. {res.written[-1].name} "
                         f"({len(res.written)} files)")
            else:
                names = ", ".join(p.name for p in res.written)
            print(f"  {flag:>4}  {src.name}  {human(before)} -> {human(total)}"
                  f"  [{res.method}]  {names}")

    if not args.quiet:
        done = len(files) - failures
        print(f"\n{done}/{len(files)} file(s) within {human(opt.target)}")
    if failures:
        print("some files could not reach the budget; try --gray, --split, "
              "or a lower --min-quality", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
