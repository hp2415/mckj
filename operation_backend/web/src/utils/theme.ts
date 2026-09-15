/** 主题色：默认企微蓝，支持用户调色并持久化。 */

export const DEFAULT_PRIMARY = "#267EF0";
const STORAGE_KEY = "op_theme_primary";

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

export function applyTheme(primaryRaw: string) {
  const primary = normalizeHex(primaryRaw);
  const root = document.documentElement;
  const rgb = hexToRgb(primary);
  const deep = mixBlack(primary, 0.18);
  const soft = mixWhite(primary, 0.88);

  root.style.setProperty("--op-brand", primary);
  root.style.setProperty("--op-brand-deep", deep);
  root.style.setProperty("--op-brand-soft", soft);
  root.style.setProperty(
    "--op-sidebar-hover",
    rgb ? `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.16)` : "rgba(38, 126, 240, 0.16)"
  );
  root.style.setProperty(
    "--op-brand-rgb",
    rgb ? `${rgb.r}, ${rgb.g}, ${rgb.b}` : "38, 126, 240"
  );

  // Element Plus
  root.style.setProperty("--el-color-primary", primary);
  root.style.setProperty("--el-color-primary-light-3", mixWhite(primary, 0.3));
  root.style.setProperty("--el-color-primary-light-5", mixWhite(primary, 0.5));
  root.style.setProperty("--el-color-primary-light-7", mixWhite(primary, 0.7));
  root.style.setProperty("--el-color-primary-light-8", mixWhite(primary, 0.8));
  root.style.setProperty("--el-color-primary-light-9", mixWhite(primary, 0.9));
  root.style.setProperty("--el-color-primary-dark-2", deep);
  root.style.setProperty("--el-border-radius-base", "10px");

  try {
    localStorage.setItem(STORAGE_KEY, primary);
  } catch {
    /* ignore */
  }
  return primary;
}

export const THEME_PRESETS = [
  { name: "企微蓝", color: "#267EF0" },
  { name: "深蓝", color: "#1A5FCC" },
  { name: "晴空", color: "#3B9AFC" },
  { name: "青绿", color: "#0D9488" },
  { name: "石墨", color: "#475569" },
];
