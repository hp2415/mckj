<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>支付方式</h4>
        <p>按成单金额</p>
      </div>
    </div>
    <div ref="el" class="chart" />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import type { ECharts } from "echarts";
import { COLOR_MODE_EVENT, chartPalette } from "../../../utils/theme";
import { CHART_COLORS, ensureChart } from "../chartUtils";

const props = defineProps<{
  items: Array<{ name: string; gmv: number; count: number }> | null;
}>();

const el = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;

function render() {
  chart = ensureChart(el.value, chart);
  if (!chart) return;
  const data = (props.items || []).map((x) => ({
    name: x.name,
    value: Number(x.gmv || 0),
  }));
  const p = chartPalette();
  chart.setOption(
    {
      color: CHART_COLORS,
      tooltip: {
        trigger: "item",
        formatter: (p: any) =>
          `${p.name}<br/>¥${Number(p.value).toLocaleString()}（${p.percent}%）`,
      },
      series: [
        {
          type: "pie",
          radius: ["40%", "68%"],
          center: ["50%", "52%"],
          label: { color: p.text, fontSize: 11 },
          data: data.length ? data : [{ name: "暂无", value: 0 }],
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

watch(() => props.items, async () => {
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
