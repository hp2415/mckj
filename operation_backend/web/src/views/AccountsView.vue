<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">账号花名册</h1>
        <p class="page-sub">按部门树展示；未分配账号在树外单独列出</p>
      </div>
      <div class="toolbar">
        <el-button v-if="auth.has('org.user.create')" type="primary" :icon="Plus" @click="showCreate = true">
          直接建号
        </el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card filter-card" shadow="never">
      <el-form :inline="true" class="filters" @submit.prevent="applySearch">
        <el-form-item label="关键词">
          <el-input
            v-model="filters.keyword"
            clearable
            placeholder="姓名 / 账号 / ID / 部门"
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
        <el-form-item label="运营角色">
          <el-select v-model="filters.op_role" clearable placeholder="全部" style="width: 130px">
            <el-option value="boss" label="boss" />
            <el-option value="manager" label="manager" />
            <el-option value="none" label="none" />
          </el-select>
        </el-form-item>
        <el-form-item label="账号">
          <el-select v-model="filters.is_active" clearable placeholder="全部" style="width: 110px">
            <el-option label="启用" value="1" />
            <el-option label="停用" value="0" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="filters.status" clearable placeholder="全部" style="width: 120px">
            <el-option value="active" label="active" />
            <el-option value="pending" label="pending" />
            <el-option value="disabled" label="disabled" />
          </el-select>
        </el-form-item>
        <el-form-item label="排序">
          <el-select v-model="sortBy" style="width: 140px">
            <el-option value="user_id" label="按 ID" />
            <el-option value="real_name" label="按姓名" />
            <el-option value="username" label="按账号" />
            <el-option value="op_role" label="按角色" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="applySearch">搜索</el-button>
          <el-button @click="resetSearch">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="unassigned.length" class="soft-card mb" shadow="never">
      <template #header>
        <div class="card-head">
          <span>未分配部门</span>
          <el-tag size="small" type="warning" effect="plain">{{ unassigned.length }} 人</el-tag>
        </div>
      </template>
      <el-table
        :data="unassigned"
        stripe
        class="roster-table"
        :default-sort="tableSort"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
      >
        <el-table-column prop="user_id" label="ID" width="80" sortable />
        <el-table-column prop="real_name" label="姓名" sortable />
        <el-table-column prop="username" label="账号" sortable />
        <el-table-column label="运营角色" width="110" prop="op_role" sortable>
          <template #default="{ row }">
            <el-tag size="small" effect="light" :type="roleTag(row.op_role)">{{ row.op_role }}</el-tag>
            <el-tag v-if="row.is_leader" size="small" type="warning" effect="plain" class="ml">主管</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账号" width="90" prop="is_active" sortable :sort-method="sortActive">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_active ? 'success' : 'danger'" effect="plain">
              {{ row.is_active ? "启用" : "停用" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110" prop="status" sortable>
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'" effect="plain">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" v-if="auth.has('org.op_role.assign')">
          <template #default="{ row }">
            <el-button size="small" @click="openRole(row)">开通/改角色</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="soft-card" shadow="never">
      <template #header>
        <div class="card-head">
          <span>部门账号树</span>
          <el-tag size="small" effect="plain">
            {{ hasFilter ? `匹配 ${matchedUserCount} 人` : "展开查看成员" }}
          </el-tag>
        </div>
      </template>
      <el-table
        :data="treeRows"
        row-key="row_key"
        default-expand-all
        :tree-props="{ children: 'children' }"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
      >
        <el-table-column label="名称" min-width="220">
          <template #default="{ row }">
            <template v-if="row.node_type === 'dept'">
              <el-text tag="b">{{ row.name }}</el-text>
              <el-tag size="small" class="ml" effect="plain">{{ row.member_count }} 人</el-tag>
              <el-tag v-if="row.kind" size="small" class="ml" type="info" effect="plain">{{ row.kind }}</el-tag>
            </template>
            <template v-else>
              {{ row.real_name || row.username }}
            </template>
          </template>
        </el-table-column>
        <el-table-column label="账号 / ID" min-width="140">
          <template #default="{ row }">
            <template v-if="row.node_type === 'user'">
              {{ row.username }}
              <el-text type="info" size="small"> #{{ row.user_id }}</el-text>
            </template>
            <el-text v-else type="info">部门</el-text>
          </template>
        </el-table-column>
        <el-table-column label="运营角色" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.node_type === 'user'" size="small" effect="light" :type="roleTag(row.op_role)">
              {{ row.op_role }}
            </el-tag>
            <el-tag v-if="row.node_type === 'user' && row.is_leader" size="small" type="warning" effect="plain" class="ml">
              主管
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账号" width="90">
          <template #default="{ row }">
            <el-tag
              v-if="row.node_type === 'user'"
              size="small"
              :type="row.is_active ? 'success' : 'danger'"
              effect="plain"
            >
              {{ row.is_active ? "启用" : "停用" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="row.node_type === 'user'"
              size="small"
              :type="row.status === 'active' ? 'success' : 'info'"
              effect="plain"
            >
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" v-if="auth.has('org.op_role.assign')">
          <template #default="{ row }">
            <el-button v-if="row.node_type === 'user'" size="small" @click="openRole(row)">开通/改角色</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="showCreate" title="直接建号" width="480px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="用户名"><el-input v-model="cform.username" /></el-form-item>
        <el-form-item label="姓名"><el-input v-model="cform.real_name" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="cform.password" type="password" show-password /></el-form-item>
        <el-form-item label="部门">
          <el-select v-model="cform.department_id" filterable style="width: 100%">
            <el-option v-for="d in deptOptions" :key="d.id" :value="d.id" :label="d.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="cform.op_role">
            <el-option value="none" label="none（待开通）" />
            <el-option value="manager" label="manager" />
            <el-option v-if="auth.user?.op_role === 'boss' || auth.user?.is_desktop_admin" value="boss" label="boss" />
          </el-select>
        </el-form-item>
        <el-form-item label="设为主管"><el-switch v-model="cform.set_leader" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" @click="createUser">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showRole" title="开通 / 改角色" width="420px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="角色">
          <el-select v-model="rform.op_role">
            <el-option value="none" label="none" />
            <el-option value="manager" label="manager" />
            <el-option v-if="auth.user?.op_role === 'boss' || auth.user?.is_desktop_admin" value="boss" label="boss" />
          </el-select>
        </el-form-item>
        <el-form-item label="部门">
          <el-select v-model="rform.department_id" filterable style="width: 100%">
            <el-option v-for="d in deptOptions" :key="d.id" :value="d.id" :label="d.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="设为主管"><el-switch v-model="rform.set_leader" /></el-form-item>
        <el-form-item label="账号启停">
          <el-switch
            v-model="rform.is_active"
            active-text="启用"
            inactive-text="停用"
            inline-prompt
          />
          <div class="hint">同步主后台 users.is_active；停用后桌面端无法登录</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRole = false">取消</el-button>
        <el-button type="primary" @click="saveRole">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { Plus, Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { useAuthStore } from "../stores/auth";
import { ElMessage } from "element-plus";
import {
  buildDeptTree,
  collectDescendantIds,
  flatDeptOptions,
  type DeptItem,
} from "../utils/deptTree";

type SortKey = "user_id" | "real_name" | "username" | "op_role";

const auth = useAuthStore();
const roster = ref<any[]>([]);
const departments = ref<DeptItem[]>([]);
const showCreate = ref(false);
const showRole = ref(false);
const currentId = ref(0);
const sortBy = ref<SortKey>("user_id");

const filters = reactive({
  keyword: "",
  department_id: undefined as number | undefined,
  op_role: "" as "" | "boss" | "manager" | "none",
  is_active: "" as "" | "0" | "1",
  status: "" as "" | "active" | "pending" | "disabled",
});
const applied = reactive({
  keyword: "",
  department_id: undefined as number | undefined,
  op_role: "" as "" | "boss" | "manager" | "none",
  is_active: "" as "" | "0" | "1",
  status: "" as "" | "active" | "pending" | "disabled",
});

const cform = reactive({
  username: "",
  real_name: "",
  password: "",
  department_id: undefined as number | undefined,
  op_role: "manager",
  set_leader: false,
});
const rform = reactive({
  op_role: "manager",
  department_id: undefined as number | undefined,
  set_leader: false,
  is_active: true,
});

const deptOptions = computed(() => flatDeptOptions(departments.value));

const matchedDeptIds = computed(() => {
  if (applied.department_id == null || applied.department_id === 0) return null;
  return collectDescendantIds(departments.value, applied.department_id);
});

const hasFilter = computed(
  () =>
    !!(
      applied.keyword.trim() ||
      applied.department_id != null ||
      applied.op_role ||
      applied.is_active !== "" ||
      applied.status
    )
);

const tableSort = computed(() => ({
  prop: sortBy.value,
  order: "ascending" as const,
}));

function matchUser(u: any): boolean {
  if (applied.department_id === 0 && u.department_id != null) return false;
  const deptIds = matchedDeptIds.value;
  if (deptIds && (u.department_id == null || !deptIds.has(u.department_id))) return false;
  if (applied.op_role && u.op_role !== applied.op_role) return false;
  if (applied.is_active === "1" && !u.is_active) return false;
  if (applied.is_active === "0" && u.is_active) return false;
  if (applied.status && u.status !== applied.status) return false;
  const q = applied.keyword.trim().toLowerCase();
  if (!q) return true;
  const name = String(u.real_name || "").toLowerCase();
  const username = String(u.username || "").toLowerCase();
  const id = String(u.user_id ?? "");
  const dept = String(u.department_name || "").toLowerCase();
  return name.includes(q) || username.includes(q) || id.includes(q) || dept.includes(q);
}

function compareUsers(a: any, b: any): number {
  const key = sortBy.value;
  if (key === "user_id") return Number(a.user_id || 0) - Number(b.user_id || 0);
  const av = String(a[key] ?? "").toLowerCase();
  const bv = String(b[key] ?? "").toLowerCase();
  if (av < bv) return -1;
  if (av > bv) return 1;
  return Number(a.user_id || 0) - Number(b.user_id || 0);
}

function sortActive(a: any, b: any) {
  return Number(!!a.is_active) - Number(!!b.is_active);
}

const filteredRoster = computed(() =>
  roster.value.filter(matchUser).slice().sort(compareUsers)
);

const unassigned = computed(() =>
  filteredRoster.value.filter((u) => u.department_id == null)
);

const matchedUserCount = computed(() => filteredRoster.value.length);

const treeRows = computed(() => {
  const byDept = new Map<number, any[]>();
  for (const u of filteredRoster.value) {
    if (u.department_id == null) continue;
    if (!byDept.has(u.department_id)) byDept.set(u.department_id, []);
    byDept.get(u.department_id)!.push({
      ...u,
      node_type: "user",
      row_key: `u-${u.user_id}`,
      children: undefined,
    });
  }
  for (const list of byDept.values()) list.sort(compareUsers);

  const deptTree = buildDeptTree(departments.value);
  const attach = (nodes: any[]): any[] =>
    nodes
      .map((d) => {
        const members = byDept.get(d.id) || [];
        const childDepts = attach(d.children || []);
        const children = [...childDepts, ...members];
        const member_count =
          members.length + childDepts.reduce((s: number, c: any) => s + (c.member_count || 0), 0);
        return {
          node_type: "dept",
          row_key: `d-${d.id}`,
          id: d.id,
          name: d.name,
          kind: d.kind,
          member_count,
          children,
        };
      })
      .filter((d) => !hasFilter.value || d.member_count > 0);
  return attach(deptTree);
});

function roleTag(role: string) {
  if (role === "boss") return "danger";
  if (role === "manager") return "success";
  return "info";
}

function applySearch() {
  applied.keyword = filters.keyword;
  applied.department_id = filters.department_id;
  applied.op_role = filters.op_role;
  applied.is_active = filters.is_active;
  applied.status = filters.status;
}

function resetSearch() {
  filters.keyword = "";
  filters.department_id = undefined;
  filters.op_role = "";
  filters.is_active = "";
  filters.status = "";
  applied.keyword = "";
  applied.department_id = undefined;
  applied.op_role = "";
  applied.is_active = "";
  applied.status = "";
  sortBy.value = "user_id";
}

async function load() {
  const [rosterRes, deptRes] = await Promise.all([
    http.get("/api/op/org/roster"),
    http.get("/api/op/org/departments"),
  ]);
  roster.value = rosterRes.data.data?.items || [];
  departments.value = deptRes.data.data?.items || [];
  if (cform.department_id == null && deptOptions.value.length) {
    cform.department_id = deptOptions.value[0].id;
  }
}

async function createUser() {
  try {
    await http.post("/api/op/org/users", cform);
    ElMessage.success("已创建");
    showCreate.value = false;
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "失败");
  }
}

function openRole(row: any) {
  currentId.value = row.user_id;
  rform.op_role = row.op_role === "boss" ? "boss" : row.op_role || "manager";
  rform.department_id = row.department_id || deptOptions.value[0]?.id;
  rform.set_leader = !!row.is_leader;
  rform.is_active = row.is_active !== false;
  showRole.value = true;
}

async function saveRole() {
  try {
    await http.post(`/api/op/org/users/${currentId.value}/role`, {
      op_role: rform.op_role,
      department_id: rform.department_id,
      set_leader: rform.set_leader,
      is_active: rform.is_active,
      status: !rform.is_active
        ? "disabled"
        : rform.op_role === "none"
          ? "pending"
          : "active",
    });
    ElMessage.success("已保存");
    showRole.value = false;
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "失败");
  }
}

onMounted(load);
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
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
.mb {
  margin-bottom: 0.85rem;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.ml {
  margin-left: 0.5rem;
}
.hint {
  margin-top: 0.35rem;
  font-size: 0.75rem;
  color: var(--op-muted, #64748b);
  line-height: 1.4;
}
.roster-table :deep(th.el-table__cell > .cell) {
  white-space: nowrap;
  line-height: 1.2;
}
.roster-table :deep(.caret-wrapper) {
  height: 14px;
  width: 16px;
}
</style>
