# input/

**Put your PDFs and images in this folder.**

Sub-folders are fine — they get picked up too. Anything that isn't a PDF or an
image (including this README) is ignored.

Accepted: `.pdf` `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tif` `.tiff` `.webp`
`.ppm` `.pgm` `.jp2`

Then from the repo root:

```bash
./run.sh
```

Compressed PDFs land in `../output/`. Your originals in here are never
modified or deleted.
