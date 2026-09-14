#!/usr/bin/env bash
#
# Compress everything in input/ into output/, one PDF per file, each under
# the size limit (150 KB by default).
#
#   ./run.sh                      # the usual case
#   ./run.sh --gray               # greyscale: much smaller for scans and text
#   ./run.sh --split              # cut documents that cannot fit into parts
#   ./run.sh --target 100kb       # a different limit
#
# Any flag accepted by compress.py can be passed through; see ./run.sh --help.

set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c 'import pymupdf, PIL' >/dev/null 2>&1; then
    echo "Installing dependencies..."
    pip3 install -q -r requirements.txt
fi

mkdir -p input output

shopt -s nullglob dotglob
contents=(input/*)
shopt -u nullglob dotglob
if [ ${#contents[@]} -le 2 ]; then   # just .gitkeep and README.md
    echo "Nothing to do: put your PDFs and images in the input/ folder first."
    exit 0
fi

exec python3 compress.py input/ -o output/ -r --target 150kb "$@"
