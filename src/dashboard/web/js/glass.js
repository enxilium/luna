// Bridge to the native overlay window. The window itself is the glass (a macOS blur of the desktop behind it); panels
// are drawn by CSS on top. In a browser (development) the window's glass is imitated over a still of the wallpaper.

const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.luna;
export const native = !!bridge;

let lastChrome = "", frame = 0;

export function post(message) { if (bridge) bridge.postMessage(message); }

// Top-bar drag: the native window drags from the bar except over its controls.
function chrome() {
  const bar = document.querySelector(".topbar");
  if (!bar) return null;
  const holes = [...bar.querySelectorAll("button, a, input, [data-hole]")].map((el) => {
    const r = el.getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
  }).filter((h) => h[2] > 0);
  return { type: "chrome", height: Math.round(bar.getBoundingClientRect().bottom), holes };
}

function flush() {
  frame = 0;
  const c = chrome();
  const json = JSON.stringify(c);
  if (c && json !== lastChrome) { lastChrome = json; post(c); }
}

// Callers pass layout hints from when panes had native glass; only the top bar's controls matter now.
export function sync() {
  if (native && !frame) frame = requestAnimationFrame(flush);
}

export function configure(style, tint) { post({ type: "glass", style, tint }); }
export function setShortcut(value) { post({ type: "shortcut", value }); }

export function install() {
  document.body.classList.add(native ? "native" : "web");
  if (!native) {
    document.body.style.setProperty("--wallpaper", 'url("/api/wallpaper")');
    return;
  }
  addEventListener("resize", () => sync());
  new MutationObserver(() => sync()).observe(document.body, { childList: true, subtree: true });
  document.fonts && document.fonts.ready.then(() => sync());
}
