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
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
      >
        <el-table-column prop="user_id" label="ID" width="70" />
        <el-table-column prop="real_name" label="姓名" />
        <el-table-column prop="username" label="账号" />
        <el-table-column label="运营角色" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="light" :type="roleTag(row.op_role)">{{ row.op_role }}</el-tag>
            <el-tag v-if="row.is_leader" size="small" type="warning" effect="plain" class="ml">主管</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账号" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_active ? 'success' : 'danger'" effect="plain">
              {{ row.is_active ? "启用" : "停用" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
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
          <el-tag size="small" effect="plain">展开查看成员</el-tag>
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
import { buildDeptTree, flatDeptOptions, type DeptItem } from "../utils/deptTree";

const auth = useAuthStore();
const roster = ref<any[]>([]);
const departments = ref<DeptItem[]>([]);
const showCreate = ref(false);
const showRole = ref(false);
const currentId = ref(0);
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

const unassigned = computed(() =>
  roster.value.filter((u) => u.department_id == null)
);

const treeRows = computed(() => {
  const byDept = new Map<number, any[]>();
  for (const u of roster.value) {
    if (u.department_id == null) continue;
    if (!byDept.has(u.department_id)) byDept.set(u.department_id, []);
    byDept.get(u.department_id)!.push({
      ...u,
      node_type: "user",
      row_key: `u-${u.user_id}`,
      children: undefined,
    });
  }
  const deptTree = buildDeptTree(departments.value);
  const attach = (nodes: any[]): any[] =>
    nodes.map((d) => {
      const members = byDept.get(d.id) || [];
      const childDepts = attach(d.children || []);
      return {
        node_type: "dept",
        row_key: `d-${d.id}`,
        id: d.id,
        name: d.name,
        kind: d.kind,
        member_count: members.length + childDepts.reduce((s: number, c: any) => s + (c.member_count || 0), 0),
        children: [...childDepts, ...members],
      };
    });
  return attach(deptTree);
});

function roleTag(role: string) {
  if (role === "boss") return "danger";
  if (role === "manager") return "success";
  return "info";
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
</style>
