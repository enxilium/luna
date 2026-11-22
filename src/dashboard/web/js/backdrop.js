// The window's backdrop: pictures from a folder chosen in Settings, one at a time, crossfading on a timer, frosted like
// the glass. Each picture is blurred once, into a small canvas that the compositor stretches to the window, so nothing
// is filtered live: page transitions and scrolling stay cheap. Without a folder the desktop behind the native window
// shows through; the browser preview stands in with a still of the wallpaper.

import { api, settings } from "./core.js";
import { native } from "./glass.js";

const root = document.getElementById("backdrop");
const layers = [document.createElement("canvas"), document.createElement("canvas")];
let images = [], order = [], index = 0, front = 0, timer = null, source = null;

for (const canvas of layers) root.appendChild(canvas);

// A box blur run three times (close to a Gaussian) over the small canvas's pixels. WebKit has no canvas filters, and
// a few dozen pixels stretched to the window without one look blocky.
function blur(ctx, w, h, r) {
  const image = ctx.getImageData(0, 0, w, h), a = image.data, b = new Uint8ClampedArray(a.length);
  const pass = (src, dst, n, m, step, stride) => {  // n lines of m pixels each
    for (let line = 0; line < n; line++) {
      for (let c = 0; c < 4; c++) {
        let sum = 0;
        const at = (i) => src[line * stride + Math.min(m - 1, Math.max(0, i)) * step + c];
        for (let i = -r; i <= r; i++) sum += at(i);
        for (let i = 0; i < m; i++) {
          dst[line * stride + i * step + c] = sum / (2 * r + 1);
          sum += at(i + r + 1) - at(i - r);
        }
      }
    }
  };
  for (let k = 0; k < 3; k++) { pass(a, b, h, w, 4, w * 4); pass(b, a, w, h, w * 4, 4); }
  ctx.putImageData(image, 0, 0);
}

// Every picture once in a shuffled order, then a new shuffle that doesn't repeat the last one first.
function next() {
  if (index >= order.length) {
    const last = order[order.length - 1];
    order = [...images].sort(() => Math.random() - 0.5);
    if (order.length > 1 && order[0] === last) order.push(order.shift());
    index = 0;
  }
  return order[index++];
}

// Cover-fit into a canvas a couple of hundred pixels wide, blurred: stretched to the window it reads as frosted glass.
function paint(canvas, img) {
  const width = 240, height = Math.max(1, Math.round(width * innerHeight / innerWidth));
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const scale = Math.max(width / img.naturalWidth, height / img.naturalHeight);
  const w = img.naturalWidth * scale, h = img.naturalHeight * scale;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, (width - w) / 2, (height - h) / 2, w, h);
  blur(ctx, width, height, settings.glass === "clear" ? 3 : 6);
}

async function show() {
  const url = next();
  if (!url) return;
  const img = new Image();
  img.src = url;
  try { await img.decode(); } catch { schedule(); return; }  // an unreadable file is skipped at the next turn
  const canvas = layers[1 - front];
  paint(canvas, img);
  canvas.classList.add("is-on");
  layers[front].classList.remove("is-on");
  front = 1 - front;
  document.body.classList.add("has-backdrop");
  schedule();
}

function schedule() {
  clearTimeout(timer);
  timer = null;
  if (images.length > 1) timer = setTimeout(show, Math.max(1, Number(settings.backdrop_interval) || 10) * 60e3);
}

// Called at boot and whenever Settings change; reloads the list only when the folder, interval or frost moved.
export async function refresh() {
  const key = `${settings.backdrop_folder || ""}|${settings.backdrop_interval}|${settings.glass}`;
  if (key === source) return;
  source = key;
  clearTimeout(timer);
  if (settings.backdrop_folder) {
    try { images = (await api.get("/api/backdrop")).images.map((n) => `/api/backdrop/image?name=${encodeURIComponent(n)}`); } catch { images = []; }
  } else images = native ? [] : ["/api/wallpaper"];
  order = []; index = 0;
  if (!images.length) { document.body.classList.remove("has-backdrop"); return; }
  show();
}

// Coming back after hours hidden: move on to a fresh picture if its time has passed.
addEventListener("luna-show", () => { if (images.length > 1 && !timer) show(); });
