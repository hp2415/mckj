<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>新增好友趋势</h4>
        <p>按加好友时间</p>
      </div>
    </div>
    <div ref="el" class="chart" />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import type { ECharts } from "echarts";
import { COLOR_MODE_EVENT } from "../../../utils/theme";
import { baseAxisStyle, brandColor, ensureChart } from "../chartUtils";

const props = defineProps<{
  trend: { labels?: string[]; friends?: number[] } | null;
}>();

const el = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;

function render() {
  chart = ensureChart(el.value, chart);
  if (!chart) return;
  const labels = props.trend?.labels || [];
  const friends = props.trend?.friends || [];
  const axis = baseAxisStyle();
  const brand = brandColor();
  chart.setOption(
    {
      color: ["#FA8A6C"],
      tooltip: { trigger: "axis" },
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      xAxis: { type: "category", data: labels, ...axis },
      yAxis: { type: "value", minInterval: 1, ...axis },
      series: [
        {
          name: "新增好友",
          type: "bar",
          data: friends,
          barWidth: labels.length > 14 ? 8 : 14,
          itemStyle: {
            borderRadius: [4, 4, 0, 0],
            color: brand,
          },
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

watch(() => props.trend, async () => {
  await nextTick();
  render();
}, { deep: true });

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
  min-height: 300px;
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
  height: 240px;
  margin-top: 0.35rem;
}
</style>
