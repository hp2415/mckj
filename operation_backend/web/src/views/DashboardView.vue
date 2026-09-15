<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">使用率大屏</h1>
        <p class="page-sub">按当前权限范围内的销售人员汇总 · 含部门层级 · 上海自然日</p>
      </div>
      <div class="toolbar">
        <el-radio-group v-model="days" size="default" @change="load">
          <el-radio-button :value="1">今天</el-radio-button>
          <el-radio-button :value="7">7 天</el-radio-button>
          <el-radio-button :value="30">30 天</el-radio-button>
        </el-radio-group>
        <el-button type="primary" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-row :gutter="14" class="kpis">
      <el-col :xs="12" :sm="8" :md="6" :lg="4" v-for="k in kpiCards" :key="k.label">
        <div class="kpi-card soft-card" :style="{ '--accent': k.color }">
          <div class="kpi-icon">
            <el-icon :size="18"><component :is="k.icon" /></el-icon>
          </div>
          <div class="kpi-title">{{ k.label }}</div>
          <div class="kpi-val">{{ k.value }}</div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="14" class="mt">
      <el-col :xs="24" :lg="14">
        <el-card class="soft-card chart-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>趋势</span>
              <el-tag size="small" effect="plain">对话 / 外发 / 登录</el-tag>
            </div>
          </template>
          <div ref="trendEl" class="chart" />
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="10">
        <el-card class="soft-card chart-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>活跃构成</span>
              <el-tag size="small" effect="plain">活跃 / 零活跃</el-tag>
            </div>
          </template>
          <div ref="pieEl" class="chart" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" class="mt">
      <el-col :xs="24" :lg="14">
        <el-card class="soft-card chart-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>销售二级部门对比</span>
              <el-tag size="small" effect="plain">综合分 / 对话 / 外发</el-tag>
            </div>
          </template>
          <div ref="deptBarEl" class="chart" />
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="10">
        <el-card class="soft-card chart-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>人员综合分 Top</span>
              <el-tag size="small" effect="plain">前 10</el-tag>
            </div>
          </template>
          <div ref="rankEl" class="chart" />
        </el-card>
      </el-col>
    </el-row>

    <el-card class="mt soft-card" shadow="never">
      <template #header>
        <div class="card-head">
          <span>部门层级</span>
          <el-tag size="small" type="info" effect="plain">父级含下级合计；未分配在树外</el-tag>
        </div>
      </template>
      <div v-if="unassigned && unassigned.member_count" class="unassigned-bar">
        <el-alert type="warning" :closable="false" show-icon>
          <template #title>
            未分配部门 {{ unassigned.member_count }} 人 · 综合分 {{ unassigned.score }} ·
            对话 {{ unassigned.chat_msgs }} · 外发 {{ unassigned.outbound_sent }} ·
            零活跃 {{ unassigned.zero_active }}
          </template>
        </el-alert>
      </div>
      <el-table
        :data="deptTree"
        row-key="id"
        default-expand-all
        :tree-props="{ children: 'children' }"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
      >
        <el-table-column prop="name" label="部门" min-width="180" />
        <el-table-column prop="member_count" label="人数" width="72" align="right" />
        <el-table-column prop="login_dau" label="登录" width="64" align="right" />
        <el-table-column prop="work_dau" label="作业" width="64" align="right" />
        <el-table-column prop="chat_msgs" label="对话" width="64" align="right" />
        <el-table-column prop="outbound_sent" label="外发" width="64" align="right" />
        <el-table-column prop="task_completed" label="任务" width="64" align="right" />
        <el-table-column prop="zero_active" label="零活跃" width="72" align="right" />
        <el-table-column label="综合分" width="88" align="right">
          <template #default="{ row }">
            <el-tag effect="light" type="success" round>{{ row.score }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="mt soft-card" shadow="never">
      <template #header>
        <div class="card-head">
          <span>人员排行</span>
          <el-tag size="small" type="info" effect="plain">点击行查看时间线</el-tag>
        </div>
      </template>
      <el-table
        :data="people"
        stripe
        class="people-table"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
        @row-click="onRow"
      >
        <el-table-column label="姓名" min-width="140">
          <template #default="{ row }">
            <div class="name-cell">
              <el-avatar :size="28" class="row-avatar">{{ (row.name || '?').slice(0, 1) }}</el-avatar>
              <div>
                <div class="n">{{ row.name }}</div>
                <div class="u">{{ row.username }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="部门" min-width="110">
          <template #default="{ row }">
            {{ row.department_name || "未分配" }}
          </template>
        </el-table-column>
        <el-table-column label="在线" width="72" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_online ? 'success' : 'info'" effect="plain">
              {{ row.is_online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="login_count" label="登录" width="64" align="right" />
        <el-table-column prop="chat_msgs" label="对话" width="64" align="right" />
        <el-table-column prop="chat_adopted" label="采纳" width="64" align="right" />
        <el-table-column prop="outbound_sent" label="外发成功" width="84" align="right" />
        <el-table-column prop="outbound_failed" label="外发失败" width="84" align="right" />
        <el-table-column label="编辑率" width="72" align="right">
          <template #default="{ row }">
            {{ row.outbound_edit_rate != null ? `${(Number(row.outbound_edit_rate) * 100).toFixed(0)}%` : "—" }}
          </template>
        </el-table-column>
        <el-table-column prop="task_completed" label="任务" width="64" align="right" />
        <el-table-column prop="blast_sent" label="群发" width="64" align="right" />
        <el-table-column prop="product_search" label="搜商品" width="72" align="right" />
        <el-table-column prop="product_copy" label="复制" width="64" align="right" />
        <el-table-column prop="product_open" label="打开" width="64" align="right" />
        <el-table-column prop="phone_dial" label="外呼" width="64" align="right" />
        <el-table-column label="综合分" width="88" align="right">
          <template #default="{ row }">
            <el-tag effect="light" type="success" round>{{ row.score }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import * as echarts from "echarts";
import { ElMessage } from "element-plus";
import {
  Refresh,
  UserFilled,
  TrendCharts,
  Warning,
  ChatDotRound,
  Promotion,
  Checked,
  Search,
  DocumentCopy,
  Phone,
  Key,
  CircleClose,
  EditPen,
  Link,
  Monitor,
} from "@element-plus/icons-vue";
import http from "../api/http";

const days = ref(7);
const loading = ref(false);
const summary = ref<any>(null);
const people = ref<any[]>([]);
const deptTree = ref<any[]>([]);
const unassigned = ref<any>(null);

const trendEl = ref<HTMLDivElement | null>(null);
const pieEl = ref<HTMLDivElement | null>(null);
const deptBarEl = ref<HTMLDivElement | null>(null);
const rankEl = ref<HTMLDivElement | null>(null);
let trendChart: echarts.ECharts | null = null;
let pieChart: echarts.ECharts | null = null;
let deptBarChart: echarts.ECharts | null = null;
let rankChart: echarts.ECharts | null = null;
const router = useRouter();
let onlineTimer: number | undefined;

function pct(rate: number | undefined) {
  if (rate == null || Number.isNaN(Number(rate))) return "—";
  return `${(Number(rate) * 100).toFixed(1)}%`;
}

const kpiCards = computed(() => {
  const d = summary.value || {};
  return [
    { label: "范围人数", value: d.scope_users ?? "—", icon: UserFilled, color: "#267EF0" },
    { label: "当前在线", value: d.online_now ?? "—", icon: Monitor, color: "#67C23A" },
    { label: "登录活跃", value: d.login_dau ?? "—", icon: Key, color: "#1A5FCC" },
    { label: "作业活跃", value: d.work_dau ?? "—", icon: TrendCharts, color: "#409EFF" },
    { label: "零活跃", value: d.zero_active ?? "—", icon: Warning, color: "#E6A23C" },
    { label: "对话消息", value: d.chat_msgs ?? "—", icon: ChatDotRound, color: "#409EFF" },
    { label: "外发成功", value: d.outbound_sent ?? "—", icon: Promotion, color: "#67C23A" },
    { label: "外发失败", value: d.outbound_failed ?? "—", icon: CircleClose, color: "#F56C6C" },
    { label: "编辑外发占比", value: pct(d.outbound_edit_rate), icon: EditPen, color: "#909399" },
    { label: "任务完成", value: d.task_completed ?? "—", icon: Checked, color: "#F56C6C" },
    { label: "商品搜索", value: d.product_search ?? "—", icon: Search, color: "#909399" },
    { label: "商品复制", value: d.product_copy ?? "—", icon: DocumentCopy, color: "#626AEF" },
    { label: "打开商品", value: d.product_open ?? "—", icon: Link, color: "#13C2C2" },
    { label: "外呼点击", value: d.phone_dial ?? "—", icon: Phone, color: "#13C2C2" },
  ];
});

function ensureChart(el: HTMLDivElement | null, existing: echarts.ECharts | null) {
  if (!el) return existing;
  if (existing) return existing;
  return echarts.init(el);
}

/** 在树中查找销售部（kind=sales 或名称含「销售」） */
function findSalesDept(nodes: any[]): any | null {
  for (const n of nodes || []) {
    const kind = String(n.kind || "").toLowerCase();
    const name = String(n.name || "");
    if (kind === "sales" || name.includes("销售")) return n;
    const hit = findSalesDept(n.children || []);
    if (hit) return hit;
  }
  return null;
}

/** 销售部下直接子部门对比（二级销售团队） */
function deptBarSeries(tree: any[]): { name: string; score: number; chat: number; outbound: number }[] {
  const sales = findSalesDept(tree || []);
  const nodes = sales?.children?.length ? sales.children : [];
  return nodes
    .map((n: any) => ({
      name: n.name,
      score: Number(n.score || 0),
      chat: Number(n.chat_msgs || 0),
      outbound: Number(n.outbound_sent || 0),
    }))
    .sort((a: any, b: any) => b.score - a.score);
}

function renderCharts() {
  const d = summary.value || {};
  const t = d.trend || { labels: [], chat: [], outbound: [], login: [] };

  trendChart = ensureChart(trendEl.value, trendChart);
  trendChart?.setOption({
    color: ["#267EF0", "#1A5FCC", "#E6A23C"],
    tooltip: { trigger: "axis" },
    legend: { data: ["对话", "外发", "登录"], top: 0 },
    grid: { left: 40, right: 20, top: 40, bottom: 28 },
    xAxis: { type: "category", data: t.labels, boundaryGap: false },
    yAxis: { type: "value", minInterval: 1, splitLine: { lineStyle: { type: "dashed", color: "#e2e8f0" } } },
    series: [
      { name: "对话", type: "line", data: t.chat, smooth: true, areaStyle: { opacity: 0.08 } },
      { name: "外发", type: "line", data: t.outbound, smooth: true, areaStyle: { opacity: 0.06 } },
      { name: "登录", type: "line", data: t.login, smooth: true },
    ],
  });

  pieChart = ensureChart(pieEl.value, pieChart);
  pieChart?.setOption({
    color: ["#267EF0", "#67C23A", "#E6A23C"],
    tooltip: { trigger: "item" },
    legend: { bottom: 0 },
    series: [
      {
        type: "pie",
        radius: ["42%", "68%"],
        center: ["50%", "46%"],
        label: { formatter: "{b}\n{c}" },
        data: [
          { name: "活跃(登录∪作业)", value: Number(d.dau_union || 0) },
          { name: "零活跃", value: Number(d.zero_active || 0) },
        ],
      },
    ],
  });

  const bars = deptBarSeries(deptTree.value);
  deptBarChart = ensureChart(deptBarEl.value, deptBarChart);
  deptBarChart?.setOption({
    color: ["#267EF0", "#409EFF", "#67C23A"],
    tooltip: { trigger: "axis" },
    legend: { data: ["综合分", "对话", "外发"], top: 0 },
    grid: { left: 48, right: 16, top: 40, bottom: 48 },
    xAxis: {
      type: "category",
      data: bars.map((b) => b.name),
      axisLabel: { interval: 0, rotate: bars.length > 4 ? 28 : 0 },
    },
    yAxis: { type: "value", minInterval: 1, splitLine: { lineStyle: { type: "dashed", color: "#e2e8f0" } } },
    series: [
      { name: "综合分", type: "bar", data: bars.map((b) => b.score), barMaxWidth: 28 },
      { name: "对话", type: "bar", data: bars.map((b) => b.chat), barMaxWidth: 28 },
      { name: "外发", type: "bar", data: bars.map((b) => b.outbound), barMaxWidth: 28 },
    ],
  });

  const top = [...people.value].slice(0, 10).reverse();
  rankChart = ensureChart(rankEl.value, rankChart);
  rankChart?.setOption({
    color: ["#267EF0"],
    tooltip: { trigger: "axis" },
    grid: { left: 88, right: 24, top: 16, bottom: 24 },
    xAxis: { type: "value", minInterval: 1, splitLine: { lineStyle: { type: "dashed", color: "#e2e8f0" } } },
    yAxis: { type: "category", data: top.map((p) => p.name || p.username) },
    series: [
      {
        type: "bar",
        data: top.map((p) => p.score),
        barMaxWidth: 18,
        label: { show: true, position: "right" },
      },
    ],
  });
}

function resizeAll() {
  trendChart?.resize();
  pieChart?.resize();
  deptBarChart?.resize();
  rankChart?.resize();
}

async function load() {
  loading.value = true;
  try {
    const { data } = await http.get("/api/op/dashboard/summary", { params: { days: days.value } });
    if (data.code !== 200) throw new Error(data.message);
    summary.value = data.data;
    people.value = data.data.people || [];
    deptTree.value = data.data.dept_tree || [];
    unassigned.value = data.data.unassigned || null;
    await nextTick();
    renderCharts();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "加载失败");
  } finally {
    loading.value = false;
  }
}

function onRow(row: any) {
  router.push(`/people/${row.user_id}`);
}

onMounted(() => {
  load();
  window.addEventListener("resize", resizeAll);
  // 在线状态依赖 last_seen，定时轻量刷新（不打断交互）
  onlineTimer = window.setInterval(() => {
    if (!loading.value) load();
  }, 30_000);
});
onUnmounted(() => {
  window.removeEventListener("resize", resizeAll);
  if (onlineTimer) window.clearInterval(onlineTimer);
  trendChart?.dispose();
  pieChart?.dispose();
  deptBarChart?.dispose();
  rankChart?.dispose();
});
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  flex-wrap: wrap;
}

.kpis {
  margin-bottom: 0.25rem;
}

.kpi-card {
  padding: 1rem 1.05rem;
  margin-bottom: 0.85rem;
  position: relative;
  overflow: hidden;
}

.kpi-card::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent);
}

.kpi-icon {
  width: 32px;
  height: 32px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: color-mix(in srgb, var(--accent) 14%, white);
  color: var(--accent);
  margin-bottom: 0.65rem;
}

.kpi-title {
  color: var(--op-muted);
  font-size: 0.8rem;
}

.kpi-val {
  font-size: 1.45rem;
  font-weight: 750;
  margin-top: 0.2rem;
  letter-spacing: -0.02em;
}

.mt {
  margin-top: 0.85rem;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}

.chart-card {
  margin-bottom: 0.85rem;
}

.chart {
  height: 300px;
}

.unassigned-bar {
  margin-bottom: 0.85rem;
}

.people-table {
  cursor: pointer;
}

.name-cell {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}

.row-avatar {
  background: linear-gradient(135deg, var(--op-brand), var(--op-brand-deep));
  color: #fff;
  font-size: 0.8rem;
}

.n {
  font-weight: 600;
}

.u {
  font-size: 0.75rem;
  color: var(--op-muted);
}
</style>
