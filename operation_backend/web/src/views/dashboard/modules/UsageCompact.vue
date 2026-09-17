<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>使用率</h4>
        <p>登录 × 作业活跃</p>
      </div>
      <router-link class="link" to="/people">人员明细 →</router-link>
    </div>
    <div class="metrics">
      <div class="m" v-for="m in metrics" :key="m.label">
        <div class="m-val">{{ m.value }}</div>
        <div class="m-label">{{ m.label }}</div>
      </div>
    </div>
    <div ref="el" class="chart" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import type { ECharts } from "echarts";
import { COLOR_MODE_EVENT, chartPalette } from "../../../utils/theme";
import { CHART_COLORS, ensureChart } from "../chartUtils";

const props = defineProps<{
  usage: Record<string, any> | null;
}>();

const el = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;

const metrics = computed(() => {
  const u = props.usage || {};
  const rate =
    u.scope_users > 0
      ? `${((Number(u.dau_union || 0) / u.scope_users) * 100).toFixed(1)}%`
      : "—";
  return [
    { label: "活跃率", value: rate },
    { label: "在线", value: u.online_now ?? "—" },
    { label: "登录", value: u.login_dau ?? "—" },
    { label: "作业", value: u.work_dau ?? "—" },
    { label: "零活跃", value: u.zero_active ?? "—" },
  ];
});

function render() {
  chart = ensureChart(el.value, chart);
  if (!chart) return;
  const mix = props.usage?.active_mix || {};
  const data = [
    { name: "仅登录", value: Number(mix.login_only || 0) },
    { name: "仅作业", value: Number(mix.work_only || 0) },
    { name: "登录+作业", value: Number(mix.both || 0) },
    { name: "零活跃", value: Number(mix.zero || 0) },
  ];
  const p = chartPalette();
  chart.setOption(
    {
      color: CHART_COLORS,
      tooltip: { trigger: "item" },
      series: [
        {
          type: "pie",
          radius: ["42%", "68%"],
          center: ["50%", "52%"],
          label: { color: p.text, fontSize: 11 },
          data,
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

watch(() => props.usage, async () => {
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

.biz-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.5rem;
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

.link {
  font-size: 0.8rem;
  color: var(--op-brand);
  text-decoration: none;
  white-space: nowrap;
}

.link:hover {
  text-decoration: underline;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 0.35rem;
  margin-top: 0.85rem;
}

.m {
  text-align: center;
  padding: 0.35rem 0.15rem;
  border-radius: 8px;
  background: color-mix(in srgb, var(--op-brand) 6%, var(--op-card));
}

.m-val {
  font-size: 0.95rem;
  font-weight: 700;
}

.m-label {
  font-size: 0.68rem;
  color: var(--op-muted);
  margin-top: 0.1rem;
}

.chart {
  height: 160px;
  margin-top: 0.35rem;
}

@media (max-width: 640px) {
  .metrics {
    grid-template-columns: repeat(3, 1fr);
  }
}
</style>
