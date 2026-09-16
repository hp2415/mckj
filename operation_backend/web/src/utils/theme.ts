/** 主题色 + 浅色/暗色模式：默认企微蓝，支持调色并持久化。 */

export const DEFAULT_PRIMARY = "#267EF0";
const STORAGE_KEY = "op_theme_primary";
const MODE_KEY = "op_color_mode";
export const COLOR_MODE_EVENT = "op-color-mode";

export type ColorMode = "light" | "dark";

function clamp(n: number, min = 0, max = 255) {
  return Math.min(max, Math.max(min, n));
}

function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const h = hex.replace("#", "").trim();
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16),
  };
}

function rgbToHex(r: number, g: number, b: number) {
  return (
    "#" +
    [r, g, b]
      .map((x) => clamp(Math.round(x)).toString(16).padStart(2, "0"))
      .join("")
  );
}

/** 与白色混合得到浅色阶 */
function mixWhite(hex: string, ratio: number) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  return rgbToHex(
    rgb.r + (255 - rgb.r) * ratio,
    rgb.g + (255 - rgb.g) * ratio,
    rgb.b + (255 - rgb.b) * ratio
  );
}

function mixBlack(hex: string, ratio: number) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  return rgbToHex(rgb.r * (1 - ratio), rgb.g * (1 - ratio), rgb.b * (1 - ratio));
}

export function normalizeHex(input: string): string {
  let h = (input || "").trim();
  if (!h.startsWith("#")) h = "#" + h;
  if (/^#[0-9a-fA-F]{3}$/.test(h)) {
    h =
      "#" +
      h
        .slice(1)
        .split("")
        .map((c) => c + c)
        .join("");
  }
  if (!/^#[0-9a-fA-F]{6}$/.test(h)) return DEFAULT_PRIMARY;
  return h.toUpperCase();
}

export function getStoredPrimary(): string {
  try {
    return normalizeHex(localStorage.getItem(STORAGE_KEY) || DEFAULT_PRIMARY);
  } catch {
    return DEFAULT_PRIMARY;
  }
}

export function isDarkMode() {
  return document.documentElement.classList.contains("dark");
}

export function getStoredColorMode(): ColorMode {
  try {
    const v = localStorage.getItem(MODE_KEY);
    if (v === "dark" || v === "light") return v;
  } catch {
    /* ignore */
  }
  return "light";
}

export function applyTheme(primaryRaw: string) {
  const primary = normalizeHex(primaryRaw);
  const root = document.documentElement;
  const rgb = hexToRgb(primary);
  const dark = isDarkMode();
  const mix = dark ? mixBlack : mixWhite;
  const deep = mixBlack(primary, 0.18);
  const soft = dark ? mixBlack(primary, 0.72) : mixWhite(primary, 0.88);

  root.style.setProperty("--op-brand", primary);
  root.style.setProperty("--op-brand-deep", deep);
  root.style.setProperty("--op-brand-soft", soft);
  root.style.setProperty(
    "--op-sidebar-hover",
    rgb
      ? `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${dark ? 0.22 : 0.16})`
      : "rgba(38, 126, 240, 0.16)"
  );
  root.style.setProperty(
    "--op-brand-rgb",
    rgb ? `${rgb.r}, ${rgb.g}, ${rgb.b}` : "38, 126, 240"
  );

  root.style.setProperty("--el-color-primary", primary);
  root.style.setProperty("--el-color-primary-light-3", mix(primary, 0.3));
  root.style.setProperty("--el-color-primary-light-5", mix(primary, 0.5));
  root.style.setProperty("--el-color-primary-light-7", mix(primary, 0.7));
  root.style.setProperty("--el-color-primary-light-8", mix(primary, 0.8));
  root.style.setProperty("--el-color-primary-light-9", mix(primary, 0.9));
  root.style.setProperty("--el-color-primary-dark-2", deep);
  root.style.setProperty("--el-border-radius-base", "10px");

  try {
    localStorage.setItem(STORAGE_KEY, primary);
  } catch {
    /* ignore */
  }
  return primary;
}

function persistMode(mode: ColorMode) {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    /* ignore */
  }
}

function setColorModeClass(mode: ColorMode) {
  document.documentElement.classList.toggle("dark", mode === "dark");
  persistMode(mode);
  applyTheme(getStoredPrimary());
  window.dispatchEvent(new CustomEvent(COLOR_MODE_EVENT, { detail: mode }));
}

function prepareViewTransition(e: MouseEvent) {
  const x = e.clientX;
  const y = e.clientY;
  const r = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y)
  );
  const root = document.documentElement;
  root.style.setProperty("--x", `${x}px`);
  root.style.setProperty("--y", `${y}px`);
  root.style.setProperty("--r", `${r}px`);
}

function disableCssTransitions() {
  const style = document.createElement("style");
  style.setAttribute("id", "op-disable-transitions");
  style.textContent = "*, *::before, *::after { transition: none !important; }";
  document.head.appendChild(style);
  return () => style.remove();
}

export function applyColorMode(mode: ColorMode, evt?: MouseEvent) {
  const run = () => setColorModeClass(mode);
  const doc = document as Document & {
    startViewTransition?: (cb: () => void) => { finished?: Promise<unknown> };
  };

  const canAnimate =
    !!evt &&
    typeof doc.startViewTransition === "function" &&
    !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  if (!canAnimate) {
    run();
    return mode;
  }

  prepareViewTransition(evt);
  const restoreTransitions = disableCssTransitions();
  try {
    const transition = doc.startViewTransition(run);
    Promise.resolve(transition?.finished).finally(restoreTransitions);
  } catch {
    restoreTransitions();
    run();
  }
  return mode;
}

export function toggleColorMode(evt?: MouseEvent) {
  return applyColorMode(isDarkMode() ? "light" : "dark", evt);
}

export function chartPalette() {
  const dark = isDarkMode();
  return {
    text: dark ? "#ababba" : "#4d5875",
    split: dark ? "#393946" : "#e2e8f0",
  };
}

export const THEME_PRESETS = [
  { name: "企微蓝", color: "#267EF0" },
  { name: "深蓝", color: "#1A5FCC" },
  { name: "晴空", color: "#3B9AFC" },
  { name: "青绿", color: "#0D9488" },
  { name: "石墨", color: "#475569" },
];
