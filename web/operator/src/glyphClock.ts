// One clock for all mounted running glyphs, scoped to their elements rather than
// the host page. Stop it when the last subscriber unmounts or motion is reduced.
const frames = ["◐", "◑", "◒", "◓"] as const;
const glyphs = new Set<HTMLElement>();
let frame = 0;
let timer: ReturnType<typeof setInterval> | undefined;
let motion: MediaQueryList | undefined;

function paint() {
  for (const glyph of glyphs) {
    glyph.dataset.char = frames[frame];
    glyph.textContent = frames[frame];
  }
}

function updateClock() {
  if (timer !== undefined) clearInterval(timer);
  timer = undefined;
  frame = 0;
  paint();
  if (!glyphs.size || motion?.matches) return;
  timer = setInterval(() => {
    if (document.hidden) return;
    frame = (frame + 1) % frames.length;
    paint();
  }, 120);
}

export function subscribeRunningGlyph(element: HTMLElement) {
  glyphs.add(element);
  element.dataset.char = frames[frame];
  element.textContent = frames[frame];
  if (glyphs.size === 1) {
    motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    motion?.addEventListener("change", updateClock);
    updateClock();
  }
  return () => {
    glyphs.delete(element);
    if (glyphs.size) return;
    if (timer !== undefined) clearInterval(timer);
    timer = undefined;
    motion?.removeEventListener("change", updateClock);
    motion = undefined;
  };
}
