# Compresser — web version

A browser version of the compressor: add PDFs or images, pick a size limit,
press Compress, download the result.

**Nothing is uploaded.** Compression happens entirely in the page, so files
never leave the device and nothing is stored on a server or in this repo.
That also means there is nothing to clean up afterwards — closing the tab
discards everything.

## Running it locally

It is a static site with no build step:

```bash
cd web && python3 -m http.server 8000
# then open http://localhost:8000
```

## How it differs from the command line tool

The browser can do most of what `compress.py` does, with one exception.

| | `compress.py` | this page |
| --- | --- | --- |
| Images | full search, lossless JPEG passthrough | same |
| PDF already under the limit | passed through untouched | same |
| PDF needing to shrink | re-encodes the embedded images, **keeping text selectable** | converts pages to images, so text stops being selectable |
| Greyscale, custom limits | yes | yes |
| Splitting a document too long to fit | `--split` | not available |

The text-preserving stage needs PyMuPDF, which has no browser equivalent. For
a long scanned document where selectable text matters, use the command line
tool.

## Sizes

A limit is read as decimal KB, so 100 KB means 100,000 bytes. Upload forms
disagree about whether a kilobyte is 1,000 or 1,024 bytes, and the smaller
reading satisfies both.
