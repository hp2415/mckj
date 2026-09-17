<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>成单趋势</h4>
        <p>金额 · 订单数</p>
      </div>
    </div>
    <div ref="el" class="chart" />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import type { ECharts } from "echarts";
import { COLOR_MODE_EVENT } from "../../../utils/theme";
import { baseAxisStyle, brandColor, ensureChart, formatAxisMoney } from "../chartUtils";

const props = defineProps<{
  trend: { labels?: string[]; gmv?: number[]; count?: number[] } | null;
}>();

const el = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;

function render() {
  chart = ensureChart(el.value, chart);
  if (!chart) return;
  const labels = props.trend?.labels || [];
  const gmv = props.trend?.gmv || [];
  const count = props.trend?.count || [];
  const axis = baseAxisStyle();
  const brand = brandColor();
  chart.setOption(
    {
      color: [brand, "#14DEBA"],
      tooltip: {
        trigger: "axis",
        formatter: (params: any) => {
          const list = Array.isArray(params) ? params : [params];
          if (!list.length) return "";
          const lines = [list[0].axisValueLabel || list[0].name];
          for (const p of list) {
            const v = Number(p.value) || 0;
            const text =
              p.seriesName === "成单金额"
                ? `¥${v.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`
                : String(v);
            lines.push(`${p.marker}${p.seriesName} ${text}`);
          }
          return lines.join("<br/>");
        },
      },
      legend: {
        data: ["成单金额", "订单数"],
        textStyle: { color: axis.axisLabel.color },
        top: 0,
        right: 0,
        itemWidth: 12,
        itemHeight: 8,
      },
      grid: { left: 52, right: 40, top: 36, bottom: 24, containLabel: true },
      xAxis: {
        type: "category",
        data: labels,
        axisLabel: { ...axis.axisLabel, hideOverlap: true },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: "value",
          ...axis,
          axisLabel: {
            color: axis.axisLabel.color,
            formatter: (v: number) => formatAxisMoney(v),
            width: 48,
            overflow: "truncate",
          },
          splitNumber: 4,
        },
        {
          type: "value",
          splitLine: { show: false },
          axisLabel: {
            color: axis.axisLabel.color,
            formatter: (v: number) => formatAxisMoney(v),
          },
          splitNumber: 4,
        },
      ],
      series: [
        {
          name: "成单金额",
          type: "line",
          smooth: true,
          showSymbol: labels.length <= 14,
          symbolSize: 6,
          data: gmv,
          areaStyle: { opacity: 0.08 },
        },
        {
          name: "订单数",
          type: "line",
          smooth: true,
          showSymbol: labels.length <= 14,
          symbolSize: 6,
          yAxisIndex: 1,
          data: count,
        },
      ],
      animation: false,
    },
    { notMerge: false, lazyUpdate: true }
  );
}

function onResize() {
  chart?.resize();
}

watch(
  () => props.trend,
  async () => {
    await nextTick();
    render();
  },
  { deep: true }
);

onMounted(async () => {
  await nextTick();
  render();
  window.addEventListener("resize", onResize);
  window.addEventListener(COLOR_MODE_EVENT, render);
});

onUnmounted(() => {
  window.removeEventListener("resize", onResize);
  window.removeEventListener(COLOR_MODE_EVENT, render);
  chart?.dispose();
  chart = null;
});
</script>

<style scoped>
.biz-card {
  background: var(--op-card);
  border: 1px solid var(--op-card-border);
  border-radius: calc(var(--op-radius) + 4px);
  padding: 1.15rem 1.25rem;
  margin-bottom: 1rem;
  box-shadow: var(--op-shadow);
  min-height: 280px;
  overflow: hidden;
}

.biz-card-header h4 {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 650;
}

.biz-card-header p {
  margin: 0.25rem 0 0;
  font-size: 0.8rem;
  color: var(--op-muted);
}

.chart {
  height: 220px;
  margin-top: 0.5rem;
  width: 100%;
  min-width: 0;
}
</style>
