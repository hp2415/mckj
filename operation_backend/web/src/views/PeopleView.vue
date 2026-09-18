<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">人员明细</h1>
        <p class="page-sub">范围内全员左连接各功能计数</p>
      </div>
      <div class="toolbar">
        <el-radio-group v-model="days" @change="() => load()">
          <el-radio-button :value="1">今日</el-radio-button>
          <el-radio-button :value="7">7 天</el-radio-button>
          <el-radio-button :value="30">30 天</el-radio-button>
        </el-radio-group>
        <el-button :icon="Refresh" :loading="loading" @click="() => load()">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card filter-card" shadow="never">
      <el-form :inline="true" class="filters" @submit.prevent="applySearch">
        <el-form-item label="关键词">
          <el-input
            v-model="filters.keyword"
            clearable
            placeholder="姓名 / 账号 / 部门"
            style="width: 200px"
            @keyup.enter="applySearch"
            @clear="applySearch"
          />
        </el-form-item>
        <el-form-item label="部门">
          <el-select
            v-model="filters.department_id"
            clearable
            filterable
            placeholder="全部"
            style="width: 200px"
          >
            <el-option :value="0" label="未分配部门" />
            <el-option v-for="d in deptOptions" :key="d.id" :value="d.id" :label="d.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="在线">
          <el-select v-model="filters.online" clearable placeholder="全部" style="width: 110px">
            <el-option label="在线" value="1" />
            <el-option label="离线" value="0" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="applySearch">搜索</el-button>
          <el-button @click="resetSearch">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="soft-card" shadow="never">
      <el-table
        v-loading="showTableLoading"
        :data="displayItems"
        stripe
        class="clickable people-table"
        :default-sort="{ prop: 'score', order: 'descending' }"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
        @row-click="(r: any) => router.push(`/people/${r.user_id}`)"
      >
        <el-table-column prop="name" label="姓名" min-width="100" sortable />
        <el-table-column prop="username" label="账号" min-width="100" sortable />
        <el-table-column prop="department_name" label="部门" min-width="120" sortable>
          <template #default="{ row }">
            {{ row.department_name || "未分配" }}
          </template>
        </el-table-column>
        <el-table-column label="在线" width="92" align="center" prop="is_online" sortable :sort-method="sortOnline">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_online ? 'success' : 'info'" effect="plain">
              {{ row.is_online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="login_count" label="登录" width="84" align="right" sortable />
        <el-table-column prop="chat_msgs" label="对话" width="84" align="right" sortable />
        <el-table-column prop="outbound_sent" label="外发成功" width="108" align="right" sortable />
        <el-table-column prop="outbound_failed" label="外发失败" width="108" align="right" sortable />
        <el-table-column
          label="编辑率"
          width="92"
          align="right"
          prop="outbound_edit_rate"
          sortable
          :sort-method="sortEditRate"
        >
          <template #default="{ row }">
            {{ row.outbound_edit_rate != null ? `${(Number(row.outbound_edit_rate) * 100).toFixed(0)}%` : "—" }}
          </template>
        </el-table-column>
        <el-table-column prop="task_completed" label="任务" width="84" align="right" sortable />
        <el-table-column prop="blast_sent" label="群发" width="84" align="right" sortable />
        <el-table-column prop="product_search" label="搜商品" width="96" align="right" sortable />
        <el-table-column prop="product_copy" label="复制" width="84" align="right" sortable />
        <el-table-column prop="product_open" label="打开" width="84" align="right" sortable />
        <el-table-column prop="phone_dial" label="外呼" width="84" align="right" sortable />
        <el-table-column prop="score" label="综合分" width="100" align="right" sortable>
          <template #default="{ row }">
            <el-tag round effect="light" type="success">{{ row.score }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="hasFilter" class="result-hint">
        共 {{ displayItems.length }} / {{ items.length }} 人
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { ElMessage } from "element-plus";
import {
  collectDescendantIds,
  flatDeptOptions,
  type DeptItem,
} from "../utils/deptTree";

const days = ref(7);
const items = ref<any[]>([]);
const departments = ref<DeptItem[]>([]);
const loading = ref(false);
const router = useRouter();
let onlineTimer: number | undefined;
let loadSeq = 0;

/** 仅首次无数据时表格转圈，避免刷新时挡住操作 */
const showTableLoading = computed(() => loading.value && items.value.length === 0);

const filters = reactive({
  keyword: "",
  online: "" as "" | "0" | "1",
  department_id: undefined as number | undefined,
});
const applied = reactive({
  keyword: "",
  online: "" as "" | "0" | "1",
  department_id: undefined as number | undefined,
});

const deptOptions = computed(() => flatDeptOptions(departments.value));

const matchedDeptIds = computed(() => {
  if (applied.department_id == null || applied.department_id === 0) return null;
  return collectDescendantIds(departments.value, applied.department_id);
});

const hasFilter = computed(
  () =>
    !!(applied.keyword.trim() || applied.online !== "" || applied.department_id != null)
);

const displayItems = computed(() => {
  const q = applied.keyword.trim().toLowerCase();
  const deptIds = matchedDeptIds.value;
  return items.value.filter((row) => {
    if (applied.online === "1" && !row.is_online) return false;
    if (applied.online === "0" && row.is_online) return false;
    if (applied.department_id === 0 && row.department_id != null) return false;
    if (deptIds && (row.department_id == null || !deptIds.has(row.department_id))) return false;
    if (!q) return true;
    const name = String(row.name || "").toLowerCase();
    const username = String(row.username || "").toLowerCase();
    const dept = String(row.department_name || "").toLowerCase();
    return name.includes(q) || username.includes(q) || dept.includes(q);
  });
});

function sortOnline(a: any, b: any) {
  return Number(!!a.is_online) - Number(!!b.is_online);
}

function sortEditRate(a: any, b: any) {
  const av = a.outbound_edit_rate == null ? -1 : Number(a.outbound_edit_rate);
  const bv = b.outbound_edit_rate == null ? -1 : Number(b.outbound_edit_rate);
  return av - bv;
}

function applySearch() {
  applied.keyword = filters.keyword;
  applied.online = filters.online;
  applied.department_id = filters.department_id;
}

function resetSearch() {
  filters.keyword = "";
  filters.online = "";
  filters.department_id = undefined;
  applied.keyword = "";
  applied.online = "";
  applied.department_id = undefined;
}

async function loadDepartments() {
  try {
    const { data } = await http.get("/api/op/org/departments");
    departments.value = data.data?.items || [];
  } catch {
    departments.value = [];
  }
}

async function load(opts?: { silent?: boolean }) {
  const silent = !!opts?.silent;
  const seq = ++loadSeq;
  if (!silent) loading.value = true;
  try {
    const { data } = await http.get("/api/op/people", { params: { days: days.value } });
    if (seq !== loadSeq) return;
    items.value = data.data?.items || [];
  } catch (e: any) {
    if (seq !== loadSeq) return;
    if (!silent) ElMessage.error(e?.response?.data?.message || "加载失败");
  } finally {
    if (seq === loadSeq && !silent) loading.value = false;
  }
}
onMounted(() => {
  loadDepartments();
  load();
  onlineTimer = window.setInterval(() => {
    if (!loading.value) load({ silent: true });
  }, 120_000);
});
onUnmounted(() => {
  if (onlineTimer) window.clearInterval(onlineTimer);
});
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}
.filter-card {
  margin-bottom: 1rem;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 0.5rem;
  align-items: center;
}
.clickable {
  cursor: pointer;
}
.people-table :deep(th.el-table__cell > .cell) {
  white-space: nowrap;
  line-height: 1.2;
}
.people-table :deep(.caret-wrapper) {
  height: 14px;
  width: 16px;
}
.people-table :deep(.sort-caret.ascending) {
  top: -2px;
}
.people-table :deep(.sort-caret.descending) {
  bottom: -2px;
}
.result-hint {
  margin-top: 0.75rem;
  font-size: 0.8125rem;
  color: var(--op-muted, #64748b);
}
</style>
