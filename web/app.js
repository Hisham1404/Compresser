/* Compresser — shrink PDFs and images to a size limit, entirely in the browser.
 *
 * The goal is to use as much of the limit as possible: a 40 KB file against a
 * 150 KB limit is a blurry file, not a good one. So nothing is fixed up front.
 * Each stage searches a ladder of settings (best quality first) for the best
 * result that still fits, then spends whatever is left on extra quality.
 */
'use strict';

pdfjsLib.GlobalWorkerOptions.workerSrc =
  'vendor/pdf.worker.min.js';

/* A size limit is read as decimal KB (100 KB = 100,000 bytes). Upload forms
 * disagree about whether KB means 1000 or 1024 bytes, and the smaller reading
 * clears both. */
const KB = 1000;

const IMAGE_SCALES = [1, .85, .72, .6, .5, .42, .35, .28, .22, .18, .14, .1, .07];
const QUALITY_STEPS = [95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10];
const QUALITY_FLOOR = 60;   // good enough to stop shrinking the image
const MIN_QUALITY = 20;

/* Rasterising ladder: [long edge in pixels, jpeg quality]. Pages are sized in
 * pixels rather than by DPI because a page box says nothing reliable about how
 * big a page really is — a scan wrapped at one pixel per point yields a box
 * several times larger than the A4 page beside it, and a single DPI would hand
 * that page most of the budget while starving the rest. */
const RASTER_LADDER = [
  [2600, 85], [2400, 82], [2200, 80], [2000, 76], [1900, 74], [1800, 72],
  [1700, 70], [1600, 68], [1500, 64], [1400, 62], [1300, 60], [1200, 56],
  [1100, 54], [1000, 50], [950, 48], [900, 45], [850, 42], [800, 40],
  [750, 38], [700, 35], [650, 32], [600, 30], [550, 27], [500, 25],
  [450, 22], [400, 20], [350, 18], [300, 15], [260, 12], [220, 10],
];

const PAGE_DPI = 150;        // assumed DPI when an image becomes a PDF page
const JPEG_IN_PDF_OVERHEAD = 4096;
const REFERENCE_PX = 2200;   // one-off render size that ladder rungs scale down from

/* ------------------------------------------------------------------ utils */

const fmtBytes = n =>
  n < 1000 ? `${n} B` : n < 1e6 ? `${(n / 1000).toFixed(1)} KB` : `${(n / 1e6).toFixed(2)} MB`;

const isPdf = f => f.type === 'application/pdf' || /\.pdf$/i.test(f.name);
const baseName = n => n.replace(/\.[^.]+$/, '');

/** Best-quality ladder rung that fits. The ladder runs best first, so size
 *  falls as the index grows and a binary search finds the leftmost fit. */
async function ladderSearch(build, ladder, budget) {
  let lo = 0, hi = ladder.length - 1, best = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const data = await build(ladder[mid]);
    if (data && data.byteLength <= budget) { best = [ladder[mid], data]; hi = mid - 1; }
    else lo = mid + 1;
  }
  return best;
}

/** Spend leftover budget on quality without changing the resolution. */
async function refineQuality(build, size, baseQuality, budget) {
  const steps = QUALITY_STEPS.filter(q => q > baseQuality);
  if (!steps.length) return null;
  return ladderSearch(q => build(size, q), steps, budget);
}

async function bestFit(build, budget, ladder) {
  const hit = await ladderSearch(s => build(s[0], s[1]), ladder, budget);
  if (!hit) return null;
  let [[size, quality], data] = hit;
  const better = await refineQuality(build, size, quality, budget);
  if (better && better[1].byteLength > data.byteLength) { quality = better[0]; data = better[1]; }
  return [[size, quality], data];
}

function canvasToJpeg(canvas, quality) {
  return new Promise(resolve => {
    canvas.toBlob(
      blob => blob ? blob.arrayBuffer().then(b => resolve(new Uint8Array(b))) : resolve(null),
      'image/jpeg', quality / 100);
  });
}

/** Draw a source onto a canvas of the given pixel size, optionally greyscale. */
function drawTo(source, w, h, gray) {
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(w));
  canvas.height = Math.max(1, Math.round(h));
  const ctx = canvas.getContext('2d', { alpha: false });
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (gray) ctx.filter = 'grayscale(1)';
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas;
}

/** EXIF orientation of a JPEG, or 1 when absent. A rotated JPEG cannot be
 *  embedded as-is, because the rotation has to be baked into the pixels. */
function jpegOrientation(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.byteLength < 4 || view.getUint16(0) !== 0xFFD8) return 1;
  let off = 2;
  while (off + 4 <= view.byteLength) {
    const marker = view.getUint16(off);
    if ((marker & 0xFF00) !== 0xFF00) return 1;
    const len = view.getUint16(off + 2);
    if (marker === 0xFFE1) {                                   // APP1
      const tiff = off + 10;
      if (tiff + 8 > view.byteLength) return 1;
      if (view.getUint32(off + 4) !== 0x45786966) return 1;     // "Exif"
      const le = view.getUint16(tiff) === 0x4949;
      const dirOffset = view.getUint32(tiff + 4, le);
      const dir = tiff + dirOffset;
      if (dir + 2 > view.byteLength) return 1;
      const count = view.getUint16(dir, le);
      for (let i = 0; i < count; i++) {
        const entry = dir + 2 + i * 12;
        if (entry + 12 > view.byteLength) break;
        if (view.getUint16(entry, le) === 0x0112) return view.getUint16(entry + 8, le) || 1;
      }
      return 1;
    }
    if (marker === 0xFFDA) return 1;                            // start of scan
    off += 2 + len;
  }
  return 1;
}

async function jpegToPdf(jpegBytes, pageW, pageH) {
  const doc = await PDFLib.PDFDocument.create();
  const img = await doc.embedJpg(jpegBytes);
  const page = doc.addPage([pageW, pageH]);
  page.drawImage(img, { x: 0, y: 0, width: pageW, height: pageH });
  return doc.save({ useObjectStreams: true });
}

/* ----------------------------------------------------------------- images */

async function compressImage(file, budget, opts, onStage) {
  const raw = new Uint8Array(await file.arrayBuffer());
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const { width: w, height: h } = bitmap;

  // The page keeps the image's original physical size, so shrinking the
  // pixels changes the file size and not the size of the printed page.
  const pageW = (w / PAGE_DPI) * 72;
  const pageH = (h / PAGE_DPI) * 72;
  const wantPdf = opts.format === 'pdf';

  // A JPEG that already fits is kept byte for byte: re-encoding it would add a
  // second round of JPEG loss and usually make it bigger.
  const upright = file.type === 'image/jpeg' && jpegOrientation(raw) === 1;
  if (upright && !opts.gray) {
    if (!wantPdf && raw.byteLength <= budget) {
      bitmap.close();
      return { bytes: raw, mime: 'image/jpeg', ext: 'jpg', method: `original kept unchanged (${w}×${h})` };
    }
    if (wantPdf && raw.byteLength + JPEG_IN_PDF_OVERHEAD <= budget) {
      const out = await jpegToPdf(raw, pageW, pageH);
      if (out.byteLength <= budget) {
        bitmap.close();
        return { bytes: out, mime: 'application/pdf', ext: 'pdf', method: `original embedded unchanged (${w}×${h})` };
      }
    }
  }

  const build = async (scale, quality) => {
    const canvas = drawTo(bitmap, w * scale, h * scale, opts.gray);
    const jpeg = await canvasToJpeg(canvas, quality);
    if (!jpeg) return null;
    return wantPdf ? await jpegToPdf(jpeg, pageW, pageH) : jpeg;
  };

  let best = null, fallback = null;
  for (const scale of IMAGE_SCALES) {
    onStage?.(`trying ${Math.round(w * scale)}×${Math.round(h * scale)}`);
    const steps = QUALITY_STEPS.filter(q => q >= MIN_QUALITY);
    const hit = await ladderSearch(q => build(scale, q), steps, budget);
    if (!hit) continue;
    const [quality, data] = hit;
    if (!fallback) fallback = { scale, quality, data };
    if (!best || quality > best.quality) best = { scale, quality, data };
    if (quality >= QUALITY_FLOOR) break;   // resolution beats sharpness
  }
  bitmap.close();

  const pick = best || fallback;
  if (!pick) {
    const data = await build(IMAGE_SCALES[IMAGE_SCALES.length - 1], MIN_QUALITY);
    return {
      bytes: data, mime: wantPdf ? 'application/pdf' : 'image/jpeg', ext: wantPdf ? 'pdf' : 'jpg',
      method: `quality ${MIN_QUALITY}, smallest available`, over: true,
    };
  }
  return {
    bytes: pick.data,
    mime: wantPdf ? 'application/pdf' : 'image/jpeg',
    ext: wantPdf ? 'pdf' : 'jpg',
    method: `quality ${pick.quality} at ${Math.round(pick.scale * 100)}% ` +
            `(${Math.round(w * pick.scale)}×${Math.round(h * pick.scale)})`,
  };
}

/* ------------------------------------------------------------------- pdfs */

async function compressPdf(file, budget, opts, onStage) {
  const raw = new Uint8Array(await file.arrayBuffer());
  if (!opts.gray && raw.byteLength <= budget) {
    return { bytes: raw, mime: 'application/pdf', ext: 'pdf', method: 'already under the limit, untouched' };
  }

  onStage?.('reading pages');
  const pdf = await pdfjsLib.getDocument({ data: raw.slice() }).promise;
  const count = pdf.numPages;

  // Render each page once at a reference size; every ladder rung then scales
  // down from that cached canvas instead of re-rendering the PDF.
  const cache = [];
  const reference = count > 30 ? Math.round(REFERENCE_PX * 0.7) : REFERENCE_PX;
  for (let n = 1; n <= count; n++) {
    onStage?.(`rendering page ${n} of ${count}`);
    const page = await pdf.getPage(n);
    const base = page.getViewport({ scale: 1 });
    const scale = Math.min(reference / Math.max(base.width, base.height), 4);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(viewport.width));
    canvas.height = Math.max(1, Math.round(viewport.height));
    const ctx = canvas.getContext('2d', { alpha: false });
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({ canvasContext: ctx, viewport }).promise;
    cache.push({ canvas, width: base.width, height: base.height });
    page.cleanup();
  }
  pdf.destroy();

  // Every page is rendered to the same long edge, so the budget is shared
  // evenly however each page box happens to be defined.
  const build = async (longEdge, quality) => {
    const doc = await PDFLib.PDFDocument.create();
    for (const item of cache) {
      const ratio = longEdge / Math.max(item.width, item.height);
      const shrunk = drawTo(item.canvas, item.width * ratio, item.height * ratio, opts.gray);
      const jpeg = await canvasToJpeg(shrunk, quality);
      if (!jpeg) return null;
      const img = await doc.embedJpg(jpeg);
      const page = doc.addPage([item.width, item.height]);
      page.drawImage(img, { x: 0, y: 0, width: item.width, height: item.height });
    }
    return doc.save({ useObjectStreams: true });
  };

  onStage?.('finding the sharpest version that fits');
  const hit = await bestFit(build, budget, RASTER_LADDER);
  cache.forEach(c => { c.canvas.width = c.canvas.height = 0; });

  if (hit) {
    const [[longEdge, quality], data] = hit;
    return {
      bytes: data, mime: 'application/pdf', ext: 'pdf',
      method: `${count} page${count > 1 ? 's' : ''} at ${longEdge}px, quality ${quality}`,
    };
  }
  const [longEdge, quality] = RASTER_LADDER[RASTER_LADDER.length - 1];
  const floor = await build(longEdge, quality);
  return {
    bytes: floor || raw, mime: 'application/pdf', ext: 'pdf',
    method: `${count} pages at ${longEdge}px — too long to fit this limit`, over: true,
  };
}

/* --------------------------------------------------------------------- ui */

const $ = id => document.getElementById(id);
const els = {
  drop: $('drop'), picker: $('picker'), queue: $('queue'), presets: $('presets'),
  size: $('size'), note: $('budget-note'), go: $('go'), status: $('status'),
  resultsCard: $('results-card'), results: $('results'), all: $('all'), clear: $('clear'),
  gray: $('gray'),
};

let files = [];
let outputs = [];

const budgetBytes = () => Math.max(5, Number(els.size.value) || 150) * KB;

function renderNote() {
  const b = budgetBytes();
  els.note.textContent =
    `Each file will be at most ${b.toLocaleString()} bytes — under the limit whether ` +
    `your form counts 1 KB as 1,000 or 1,024 bytes.`;
}

function renderQueue() {
  els.queue.hidden = files.length === 0;
  els.queue.innerHTML = '';
  files.forEach((file, i) => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'nm'; name.textContent = file.name;
    const size = document.createElement('span');
    size.className = 'sz'; size.textContent = fmtBytes(file.size);
    const remove = document.createElement('button');
    remove.className = 'x'; remove.type = 'button';
    remove.setAttribute('aria-label', `Remove ${file.name}`);
    remove.textContent = '×';
    remove.onclick = () => { files.splice(i, 1); renderQueue(); };
    li.append(name, size, remove);
    els.queue.append(li);
  });
  els.go.disabled = files.length === 0;
  els.go.textContent = files.length > 1 ? `Compress ${files.length} files` : 'Compress';
}

function addFiles(list) {
  const accepted = [...list].filter(f =>
    isPdf(f) || f.type.startsWith('image/') || /\.(jpe?g|png|webp|bmp|tiff?)$/i.test(f.name));
  const rejected = [...list].length - accepted.length;
  files = files.concat(accepted);
  renderQueue();
  els.status.textContent = rejected ? `${rejected} file(s) skipped — not a PDF or image.` : '';
}

els.drop.onclick = () => els.picker.click();
els.drop.onkeydown = e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); els.picker.click(); }
};
els.picker.onchange = () => { addFiles(els.picker.files); els.picker.value = ''; };
['dragenter', 'dragover'].forEach(t => els.drop.addEventListener(t, e => {
  e.preventDefault(); els.drop.classList.add('over');
}));
['dragleave', 'drop'].forEach(t => els.drop.addEventListener(t, e => {
  e.preventDefault(); els.drop.classList.remove('over');
}));
els.drop.addEventListener('drop', e => { if (e.dataTransfer?.files) addFiles(e.dataTransfer.files); });

els.presets.onclick = e => {
  const chip = e.target.closest('.chip');
  if (!chip) return;
  els.size.value = chip.dataset.kb;
  [...els.presets.children].forEach(c => c.classList.toggle('is-on', c === chip));
  renderNote();
};
els.size.oninput = () => {
  [...els.presets.children].forEach(c => c.classList.toggle('is-on', c.dataset.kb === els.size.value));
  renderNote();
};

function download(out) {
  const url = URL.createObjectURL(new Blob([out.bytes], { type: out.mime }));
  const a = document.createElement('a');
  a.href = url; a.download = out.filename;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function addResult(out) {
  const li = document.createElement('li');
  li.className = 'res';

  const top = document.createElement('div');
  top.className = 'res-top';
  const name = document.createElement('span');
  name.className = 'res-name'; name.textContent = out.filename;
  const badge = document.createElement('span');
  badge.className = `badge ${out.error ? 'err' : out.over ? 'over' : 'ok'}`;
  badge.textContent = out.error ? 'failed' : out.over ? 'too big' : 'ready';
  top.append(name, badge);
  li.append(top);

  if (out.error) {
    const meta = document.createElement('div');
    meta.className = 'meta'; meta.textContent = out.error;
    li.append(meta);
  } else {
    const sizes = document.createElement('div');
    sizes.className = 'sizes';
    const before = document.createElement('span');
    before.textContent = fmtBytes(out.before);
    const arrow = document.createElement('span');
    arrow.className = 'arrow'; arrow.textContent = '→';
    const after = document.createElement('span');
    after.className = 'new';
    after.textContent = `${fmtBytes(out.bytes.byteLength)} (${out.bytes.byteLength.toLocaleString()} bytes)`;
    sizes.append(before, arrow, after);

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = out.over
      ? `${out.method}. Try a bigger limit, or greyscale.`
      : out.method;

    const button = document.createElement('button');
    button.className = 'dl'; button.type = 'button';
    button.textContent = 'Download';
    button.onclick = () => download(out);

    li.append(sizes, meta, button);
  }
  els.results.append(li);
}

els.go.onclick = async () => {
  if (!files.length) return;
  const budget = budgetBytes();
  const opts = {
    gray: els.gray.checked,
    format: document.querySelector('input[name="fmt"]:checked').value,
  };

  els.go.disabled = true;
  els.results.innerHTML = '';
  outputs = [];
  els.resultsCard.hidden = false;
  els.all.hidden = true;

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    const label = files.length > 1 ? `(${i + 1}/${files.length}) ` : '';
    const stage = text => { els.status.textContent = `${label}${file.name} — ${text}…`; };
    stage('starting');
    try {
      const result = isPdf(file)
        ? await compressPdf(file, budget, opts, stage)
        : await compressImage(file, budget, opts, stage);
      const out = { ...result, before: file.size, filename: `${baseName(file.name)}.${result.ext}` };
      outputs.push(out);
      addResult(out);
    } catch (err) {
      console.error(err);
      addResult({ filename: file.name, error: err.message || String(err) });
    }
    await new Promise(r => setTimeout(r, 0));   // let the page repaint
  }

  const done = outputs.filter(o => !o.over).length;
  els.status.textContent = `Done — ${done} of ${files.length} within ${budget.toLocaleString()} bytes.`;
  els.all.hidden = outputs.length < 2;
  els.go.disabled = false;
};

els.all.onclick = () => outputs.forEach((out, i) => setTimeout(() => download(out), i * 350));

els.clear.onclick = () => {
  files = []; outputs = [];
  els.results.innerHTML = '';
  els.resultsCard.hidden = true;
  els.status.textContent = '';
  renderQueue();
};

renderNote();
renderQueue();
