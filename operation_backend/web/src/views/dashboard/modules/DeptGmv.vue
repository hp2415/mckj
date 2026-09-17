<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div class="head-main">
        <h4>部门成单</h4>
        <p>
          {{ rootHint }}
          <span v-if="hasUnassigned"> · 含未归属</span>
        </p>
      </div>
      <el-cascader
        v-if="showRootPicker"
        v-model="rootPath"
        :options="rootOptions"
        :props="cascaderProps"
        clearable
        filterable
        size="small"
        placeholder="对比根部门"
        class="root-picker"
        @change="onRootChange"
      />
    </div>
    <div ref="el" class="chart" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import type { ECharts } from "echarts";
import { COLOR_MODE_EVENT } from "../../../utils/theme";
import {
  baseAxisStyle,
  brandColor,
  ensureChart,
  formatAxisMoney,
  formatMoneyShort,
} from "../chartUtils";

const props = defineProps<{
  items: Array<{ id?: number | null; name: string; gmv: number; count: number }> | null;
  rootDeptId?: number | null;
  rootDeptName?: string | null;
  deptOptions?: any[];
  /** 页头已筛部门/未分配时不展示独立根选择 */
  allowPickRoot?: boolean;
}>();

const emit = defineEmits<{
  (e: "update:rootDeptId", id: number | null): void;
}>();

const el = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;

const cascaderProps = {
  value: "value",
  label: "label",
  children: "children",
  checkStrictly: true,
  emitPath: true,
};

const rootPath = ref<(number | string)[]>([]);
const rootOptions = computed(() => props.deptOptions || []);
const showRootPicker = computed(
  () => !!props.allowPickRoot && (props.deptOptions || []).length > 0
);

const hasUnassigned = computed(() =>
  (props.items || []).some((x) => x.name === "未归属")
);

const rootHint = computed(() => {
  const name = props.rootDeptName;
  if (name) return `${name} · 下级对比`;
  return "含下级合计";
});

watch(
  () => props.rootDeptId,
  (id) => {
    if (id == null) {
      rootPath.value = [];
      return;
    }
    // 尽量保留已有路径末级；否则只设末级 id（级联仍可展示）
    const last = rootPath.value?.[rootPath.value.length - 1];
    if (Number(last) !== Number(id)) {
      rootPath.value = [id];
    }
  },
  { immediate: true }
);

function onRootChange() {
  const last = rootPath.value?.[rootPath.value.length - 1];
  if (last == null || last === "") {
    emit("update:rootDeptId", null);
    return;
  }
  emit("update:rootDeptId", Number(last));
}

function render() {
  chart = ensureChart(el.value, chart);
  if (!chart) return;
  const items = [...(props.items || [])].slice(0, 10).reverse();
  const axis = baseAxisStyle();
  chart.setOption(
    {
      color: [brandColor()],
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params: any) => {
          const p = Array.isArray(params) ? params[0] : params;
          const row = items[p.dataIndex];
          return `${p.name}<br/>金额 ${formatMoneyShort(Number(p.value))}<br/>订单 ${row?.count ?? 0}`;
        },
      },
      grid: { left: 8, right: 28, top: 12, bottom: 12, containLabel: true },
      xAxis: {
        type: "value",
        ...axis,
        axisLabel: {
          color: axis.axisLabel.color,
          formatter: (v: number) => formatAxisMoney(v),
        },
        splitNumber: 4,
      },
      yAxis: {
        type: "category",
        data: items.map((x) => x.name),
        axisLabel: {
          color: axis.axisLabel.color,
          width: 64,
          overflow: "truncate",
        },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [
        {
          type: "bar",
          data: items.map((x) => x.gmv),
          barWidth: 14,
          itemStyle: { borderRadius: [0, 6, 6, 0] },
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
  () => props.items,
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
  min-height: 300px;
  overflow: hidden;
}

.biz-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.head-main {
  min-width: 0;
  flex: 1;
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

.root-picker {
  width: 160px;
  flex-shrink: 0;
}

.chart {
  height: 240px;
  margin-top: 0.35rem;
  width: 100%;
  min-width: 0;
}
</style>
