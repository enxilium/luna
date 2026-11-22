// One audio player for the whole app, so a bounce keeps playing while you move between tabs.

const audio = new Audio();
audio.preload = "metadata";
const listeners = new Set();
export const now = { track: null, playing: false, time: 0, duration: 0 };
const peaksCache = new Map();

const emit = () => { for (const fn of listeners) fn(now); };
audio.addEventListener("timeupdate", () => { now.time = audio.currentTime; emit(); });
audio.addEventListener("loadedmetadata", () => { now.duration = audio.duration; emit(); });
audio.addEventListener("play", () => { now.playing = true; emit(); });
audio.addEventListener("pause", () => { now.playing = false; emit(); });
audio.addEventListener("ended", () => { now.playing = false; now.time = 0; emit(); });

export const onPlayer = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
export const fileUrl = (path) => `/api/file?path=${encodeURIComponent(path)}`;

export function play(track) {
  if (now.track && now.track.path === track.path) { toggle(); return; }
  now.track = track; now.time = 0; now.duration = 0;
  audio.src = fileUrl(track.path);
  audio.play().catch(() => { now.playing = false; emit(); });
  emit();
}
export function toggle() { if (!now.track) return; audio.paused ? audio.play() : audio.pause(); }
export function seek(fraction) { if (now.duration) audio.currentTime = fraction * now.duration; }
export function stop() { audio.pause(); now.track = null; emit(); }
export const isCurrent = (path) => now.track && now.track.path === path;

// Waveform peaks, decoded once per file. Large files are skipped rather than stalling the UI.
export async function peaks(path, size, count = 96) {
  const key = `${path}:${count}`;
  if (peaksCache.has(key)) return peaksCache.get(key);
  if (size > 80 * 1024 * 1024) return null;
  const job = (async () => {
    const response = await fetch(fileUrl(path));
    const buffer = await response.arrayBuffer();
    const context = new (window.AudioContext || window.webkitAudioContext)();
    try {
      const decoded = await context.decodeAudioData(buffer);
      const data = decoded.getChannelData(0);
      const step = Math.floor(data.length / count) || 1;
      const out = [];
      for (let i = 0; i < count; i++) {
        let peak = 0;
        for (let j = i * step; j < Math.min(data.length, (i + 1) * step); j += 16) peak = Math.max(peak, Math.abs(data[j]));
        out.push(peak);
      }
      const max = Math.max(...out) || 1;
      return { peaks: out.map((p) => p / max), duration: decoded.duration };
    } finally {
      context.close();
    }
  })().catch(() => null);
  peaksCache.set(key, job);
  return job;
}
