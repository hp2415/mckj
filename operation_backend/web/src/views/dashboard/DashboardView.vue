<template>
  <div class="dash-page" :class="{ 'is-refreshing': silentLoading }">
    <div class="page-head">
      <div>
        <h1 class="page-title">经营大屏</h1>
        <p class="page-sub">{{ pageSub }}</p>
      </div>
      <div class="toolbar">
        <el-cascader
          v-if="isAdminViewer"
          v-model="deptPath"
          :options="deptCascaderOptions"
          :props="cascaderProps"
          clearable
          filterable
          placeholder="全部部门"
          style="width: 220px"
          @change="onScopeChange"
        />
        <el-radio-group v-model="days" size="default" @change="onScopeChange">
          <el-radio-button :value="1">今天</el-radio-button>
          <el-radio-button :value="7">7 天</el-radio-button>
          <el-radio-button :value="30">30 天</el-radio-button>
        </el-radio-group>
        <el-button type="primary" :icon="Refresh" :loading="loading" @click="() => load(false)">
          刷新
        </el-button>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :xl="14" :lg="15" :xs="24">
        <BizKpis :kpis="overview?.kpis || null" :days="days" />
      </el-col>
      <el-col :xl="10" :lg="9" :xs="24">
        <OrderTrend :trend="overview?.order_trend || null" />
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :xl="10" :lg="10" :xs="24">
        <DeptGmv
          :items="overview?.dept_gmv || null"
          :root-dept-id="gmvRootDeptId"
          :root-dept-name="overview?.gmv_root_dept_name || null"
          :dept-options="deptCascaderOptionsNoUnassigned"
          :allow-pick-root="allowGmvRootPick"
          @update:root-dept-id="onGmvRootChange"
        />
      </el-col>
      <el-col :xl="7" :lg="7" :xs="24">
        <UsageCompact :usage="overview?.usage || null" />
      </el-col>
      <el-col :xl="7" :lg="7" :xs="24">
        <PayTypeRing :items="overview?.pay_types || null" />
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :xl="10" :lg="10" :xs="24">
        <TopStaff :items="overview?.top_staff || null" />
      </el-col>
      <el-col :xl="7" :lg="7" :xs="24">
        <CallStats :calls="overview?.calls || null" />
      </el-col>
      <el-col :xl="7" :lg="7" :xs="24">
        <FriendTrend :trend="overview?.friend_trend || null" />
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import http from "../../api/http";
import { useAuthStore } from "../../stores/auth";
import { buildDeptTree } from "../../utils/deptTree";
import BizKpis from "./modules/BizKpis.vue";
import OrderTrend from "./modules/OrderTrend.vue";
import DeptGmv from "./modules/DeptGmv.vue";
import UsageCompact from "./modules/UsageCompact.vue";
import PayTypeRing from "./modules/PayTypeRing.vue";
import TopStaff from "./modules/TopStaff.vue";
import CallStats from "./modules/CallStats.vue";
import FriendTrend from "./modules/FriendTrend.vue";

const auth = useAuthStore();
const isAdminViewer = computed(
  () => !!auth.user?.is_desktop_admin || auth.user?.op_role === "boss"
);

const days = ref(7);
const loading = ref(false);
const silentLoading = ref(false);
const overview = ref<any>(null);
const deptPath = ref<(number | string)[]>([]);
const deptFlat = ref<any[]>([]);
/** 部门成单对比根；null 表示交给后端默认（销售部） */
const gmvRootDeptId = ref<number | null>(null);
const gmvRootTouched = ref(false);

const cascaderProps = {
  value: "value",
  label: "label",
  children: "children",
  checkStrictly: true,
  emitPath: true,
};

const selectedDeptId = computed(() => {
  const last = deptPath.value?.[deptPath.value.length - 1];
  if (last === "unassigned" || last == null || last === "") return null;
  return Number(last);
});

const isUnassignedFilter = computed(
  () => deptPath.value?.[deptPath.value.length - 1] === "unassigned"
);

const allowGmvRootPick = computed(
  () => isAdminViewer.value && !isUnassignedFilter.value && selectedDeptId.value == null
);

function toCascaderNodes(nodes: any[]): any[] {
  return (nodes || []).map((n) => ({
    value: n.id,
    label: n.name,
    children: n.children?.length ? toCascaderNodes(n.children) : undefined,
  }));
}

const deptCascaderOptions = computed(() => {
  const tree = buildDeptTree(deptFlat.value);
  const opts = toCascaderNodes(tree);
  opts.push({ value: "unassigned", label: "未分配", children: undefined });
  return opts;
});

const deptCascaderOptionsNoUnassigned = computed(() =>
  toCascaderNodes(buildDeptTree(deptFlat.value))
);

const pageSub = computed(() => {
  const scope = overview.value?.scope;
  if (isAdminViewer.value) {
    const name = scope?.dept_name || (isUnassignedFilter.value ? "未分配" : "全部部门");
    return `管理员视角 · ${name}（含下级）· 上海自然日`;
  }
  return "本部门范围 · 上海自然日";
});

async function loadDepts() {
  if (!isAdminViewer.value) return;
  try {
    const { data } = await http.get("/api/op/org/departments");
    if (data.code === 200) {
      deptFlat.value = data.data?.items || [];
    }
  } catch {
    /* 无部门权限时忽略 */
  }
}

function onScopeChange() {
  // 页头范围变化时，重置图表根（全部视角重新默认销售部）
  gmvRootTouched.value = false;
  gmvRootDeptId.value = selectedDeptId.value;
  load(false);
}

function onGmvRootChange(id: number | null) {
  gmvRootTouched.value = true;
  gmvRootDeptId.value = id;
  load(false);
}

async function load(silent = false) {
  if (silent) {
    if (loading.value || silentLoading.value) return;
    silentLoading.value = true;
  } else {
    loading.value = true;
  }
  try {
    const params: Record<string, any> = { days: days.value };
    if (isAdminViewer.value) {
      if (isUnassignedFilter.value) params.unassigned = true;
      else if (selectedDeptId.value != null) params.dept_id = selectedDeptId.value;
    }
    // 全部视角可指定柱图根；未手动选过则不传，后端默认销售部
    if (allowGmvRootPick.value && gmvRootTouched.value && gmvRootDeptId.value != null) {
      params.gmv_root_dept_id = gmvRootDeptId.value;
    }
    const { data } = await http.get("/api/op/dashboard/overview", { params });
    if (data.code !== 200) throw new Error(data.message);
    overview.value = data.data;
    if (data.data?.gmv_root_dept_id != null && !gmvRootTouched.value) {
      gmvRootDeptId.value = data.data.gmv_root_dept_id;
    }
  } catch (e: any) {
    if (!silent) {
      ElMessage.error(
        e?.response?.data?.detail || e?.response?.data?.message || e?.message || "加载失败"
      );
    }
  } finally {
    loading.value = false;
    silentLoading.value = false;
  }
}

let onlineTimer: number | undefined;

onMounted(async () => {
  await loadDepts();
  await load(false);
  onlineTimer = window.setInterval(() => {
    load(true);
  }, 30_000);
});

onUnmounted(() => {
  if (onlineTimer) window.clearInterval(onlineTimer);
});
</script>

<style scoped>
.dash-page {
  min-height: 200px;
}

.dash-page.is-refreshing {
  /* 静默刷新时不挡交互，仅轻微提示 */
  opacity: 1;
}

.toolbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  flex-wrap: wrap;
}
</style>
