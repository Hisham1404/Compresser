# Compresser

Turns PDFs and images into **separate PDFs that each stay under a size limit** —
150 KB by default. Point it at files or folders; every input becomes its own
output PDF.

```
photo.jpg      7.2 MB  ->  photo.pdf        147 KB
scan.pdf       1.2 MB  ->  scan.pdf         147 KB
invoice.png    340 KB  ->  invoice.pdf       92 KB
```

## The website

A browser version lives in [`web/`](web/) and is deployed at:

**https://compresser-hisham1404s-projects.vercel.app**

Add files, pick a size limit, press Compress, download. It is mobile friendly,
and compression happens entirely in the page — files are never uploaded, so
nothing is stored on a server or in this repo and there is nothing to clean up
afterwards.

> The Vercel project is created with Deployment Protection on, so the link asks
> for a Vercel login. To open it to everyone: Vercel dashboard → the
> `compresser` project → Settings → Deployment Protection → turn off Vercel
> Authentication.

## Quick start: the input/ and output/ folders

The simplest way to use this. Drop your files into `input/`, run one command,
collect the results from `output/`:

```bash
# 1. put your PDFs and images in input/  (sub-folders are fine)
# 2. run:
./run.sh
# 3. the compressed PDFs are in output/, one per file, each under 150 KB
```

`run.sh` installs the dependencies on first run. Your originals in `input/`
are never modified or deleted, and re-running overwrites the previous results
rather than piling up copies.

Flags pass straight through:

```bash
./run.sh --gray            # greyscale: much smaller for scans and text
./run.sh --split           # cut documents too long to fit into parts
./run.sh --target 100kb    # a different size limit
```

## Install

```bash
pip install -r requirements.txt
```

Pure Python wheels — no Ghostscript, ImageMagick or other system tools needed.

## Use directly

For any folder, not just `input/`:

```bash
# everything in a folder, into ./compressed/
python3 compress.py scans/

# specific files, custom limit and output folder
python3 compress.py photo.jpg report.pdf -o out/ --target 100kb

# folders and sub-folders, greyscale (much smaller for text and scans)
python3 compress.py documents/ -r --gray

# split a document that cannot possibly fit into numbered parts
python3 compress.py 200-page-scan.pdf --split
```

Exit status is `0` when every file met the budget and `1` when one did not, so
it drops straight into a script:

```bash
python3 compress.py inbox/ -o outbox/ || echo "some files need --gray or --split"
```

### Options

| Option | Default | What it does |
| --- | --- | --- |
| `-o, --out DIR` | `compressed` | Where the PDFs are written |
| `-t, --target SIZE` | `150kb` | Maximum size per output PDF (`150kb`, `1.5mb`, `204800`) |
| `-r, --recursive` | off | Descend into sub-folders |
| `--gray` | off | Convert to greyscale — a large saving on text and scans |
| `--split` | off | Split a PDF that cannot fit into `name-part1.pdf`, `name-part2.pdf`, … |
| `--page-dpi N` | `150` | Assumed DPI when turning an image into a page |
| `--page-fit {auto,a4}` | `auto` | `auto` keeps the image's aspect ratio, `a4` fits it onto A4 |
| `--min-quality N` | `20` | Lowest JPEG quality the search may use |
| `--quality-floor N` | `60` | Quality considered good enough to stop downscaling |
| `-j, --jobs N` | CPU count | Files processed in parallel |
| `-q, --quiet` | off | Only print problems |
| `--debug` | off | Show errors that are otherwise handled per file |

Supported input: `.pdf`, plus `.jpg .jpeg .png .gif .bmp .tif .tiff .webp .ppm
.pgm .jp2`. Install `pillow-heif` to add `.heic` / `.heif`.

## How it gets under the limit

The goal is to use as much of the budget as possible — a 40 KB file against a
150 KB budget is a blurry file, not a good one. So nothing is fixed up front:
each stage searches a ladder of settings (best quality first) and keeps the
best result that still fits, then spends any leftover budget on extra quality
at the same resolution.

**Images** are scaled down progressively; at each size the highest JPEG quality
that fits is found by binary search. The first size that still supports a
decent quality wins, so resolution is preferred over sharpness. The page keeps
the image's original aspect ratio and physical dimensions — downscaling changes
the file size, not the size of the printed page.

**PDFs** go through up to four stages, stopping as soon as one fits:

1. **Copy** — already under the limit, so nothing is touched.
2. **Lossless cleanup** — subset fonts, pack object streams, drop unused
   objects. Nothing is re-encoded, so the file is untouched visually.
3. **Re-encode the embedded images** — the pictures shrink but text and vectors
   stay sharp and selectable. Preferred whenever the document has a text layer.
4. **Rasterise** — each page becomes a single JPEG. This is a last resort: it
   tracks the budget closely but the text stops being selectable, so it is only
   used when there is no text layer to lose, or when nothing else fits.

If even the lowest setting is too big — usually a long scanned document —
the file is reported as `OVER` and the run exits non-zero. `--split` instead
cuts it into numbered parts that each fit, sizing each part so it is feasible
and then raising its quality to use the room available.

## Tests

```bash
python3 test_compress.py
```

25 tests covering both file types, the size search, greyscale, splitting,
recursion, name collisions, damaged input, and checks that a compressed
document keeps its page count and its text layer.
