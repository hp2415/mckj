<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">邀请码</h1>
        <p class="page-sub">生成后发给新同学；可绑定部门与授予角色</p>
      </div>
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate">生成邀请码</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card" shadow="never">
      <el-table :data="items" stripe :header-cell-style="{ background: '#f8fafc', color: '#475569' }">
        <el-table-column prop="code" label="邀请码" width="140">
          <template #default="{ row }">
            <el-text tag="b" type="primary">{{ row.code }}</el-text>
          </template>
        </el-table-column>
        <el-table-column label="部门" min-width="140">
          <template #default="{ row }">
            {{ row.department_name || "—" }}
          </template>
        </el-table-column>
        <el-table-column prop="grant_op_role" label="授予角色" width="100" />
        <el-table-column label="用量" width="110">
          <template #default="{ row }">
            {{ row.used_count }}/{{ row.max_uses }}
          </template>
        </el-table-column>
        <el-table-column prop="expires_at" label="过期" min-width="150" />
        <el-table-column prop="note" label="备注" min-width="120" />
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button size="small" :icon="CopyDocument" @click="copy(row.code)">复制</el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="!!row.revoked_at"
              @click="revoke(row.id)"
            >
              停用
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="show" title="生成邀请码" width="440px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="部门">
          <el-select v-model="form.department_id" filterable style="width: 100%">
            <el-option v-for="d in deptOptions" :key="d.id" :value="d.id" :label="d.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="授予角色">
          <el-select v-model="form.grant_op_role" style="width: 100%">
            <el-option value="none" label="none（待开通）" />
            <el-option value="manager" label="manager（可登录）" />
          </el-select>
        </el-form-item>
        <el-form-item label="可用次数"><el-input-number v-model="form.max_uses" :min="1" /></el-form-item>
        <el-form-item label="有效天数"><el-input-number v-model="form.expires_days" :min="1" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="form.note" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="show = false">取消</el-button>
        <el-button type="primary" @click="create">生成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { Plus, Refresh, CopyDocument } from "@element-plus/icons-vue";
import http from "../api/http";
import { ElMessage } from "element-plus";
import { flatDeptOptions, type DeptItem } from "../utils/deptTree";

const items = ref<any[]>([]);
const departments = ref<DeptItem[]>([]);
const show = ref(false);
const form = reactive({
  department_id: undefined as number | undefined,
  grant_op_role: "manager",
  max_uses: 1,
  expires_days: 7,
  note: "",
});

const deptOptions = computed(() => flatDeptOptions(departments.value));

function openCreate() {
  if (form.department_id == null && deptOptions.value.length) {
    form.department_id = deptOptions.value[0].id;
  }
  show.value = true;
}

async function load() {
  const [invRes, deptRes] = await Promise.all([
    http.get("/api/op/org/invites"),
    http.get("/api/op/org/departments"),
  ]);
  items.value = invRes.data.data?.items || [];
  departments.value = deptRes.data.data?.items || [];
}

async function create() {
  try {
    const { data } = await http.post("/api/op/org/invites", form);
    ElMessage.success(`已生成：${data.data?.code}`);
    show.value = false;
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "失败");
  }
}

async function revoke(id: number) {
  await http.post(`/api/op/org/invites/${id}/revoke`);
  ElMessage.success("已停用");
  await load();
}

async function copy(code: string) {
  try {
    await navigator.clipboard.writeText(code);
    ElMessage.success("已复制");
  } catch {
    ElMessage.info(code);
  }
}

onMounted(load);
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
}
</style>
