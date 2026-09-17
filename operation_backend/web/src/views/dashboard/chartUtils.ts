import * as echarts from "echarts";
import { chartPalette } from "../../utils/theme";

export function ensureChart(el: HTMLDivElement | null, existing: echarts.ECharts | null) {
  if (!el) return existing;
  if (existing && !existing.isDisposed()) return existing;
  return echarts.init(el);
}

export function baseAxisStyle() {
  const p = chartPalette();
  return {
    axisLabel: { color: p.text },
    axisLine: { show: false },
    splitLine: { lineStyle: { color: p.split, type: "dashed" as const } },
  };
}

export function brandColor() {
  return (
    getComputedStyle(document.documentElement).getPropertyValue("--op-brand").trim() ||
    "#267EF0"
  );
}

/** 金额轴：过万显示为万，避免长数字撑破 */
export function formatAxisMoney(v: number) {
  const n = Number(v) || 0;
  const abs = Math.abs(n);
  if (abs >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (abs >= 10000) return `${(n / 10000).toFixed(1)}万`;
  if (abs >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}

export function formatMoneyShort(n: number | undefined | null) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const abs = Math.abs(v);
  if (abs >= 100000000) return `¥${(v / 100000000).toFixed(2)}亿`;
  if (abs >= 10000) return `¥${(v / 10000).toFixed(2)}万`;
  return `¥${v.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
}

export const CHART_COLORS = ["#267EF0", "#14DEBA", "#FFAF20", "#FA8A6C", "#4ABEFF", "#626AEF"];
