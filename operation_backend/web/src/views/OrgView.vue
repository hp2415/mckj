<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">部门树</h1>
        <p class="page-sub">支持新建 / 编辑 / 删除；有子部门或成员时不可删</p>
      </div>
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate()">新建子部门</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card" shadow="never">
      <el-table
        :data="tree"
        row-key="id"
        default-expand-all
        :tree-props="{ children: 'children' }"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
      >
        <el-table-column prop="name" label="名称" min-width="200" />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.kind" size="small" effect="plain">{{ row.kind }}</el-tag>
            <el-text v-else type="info">继承</el-text>
          </template>
        </el-table-column>
        <el-table-column label="主管" min-width="140">
          <template #default="{ row }">
            <span v-if="row.leader_name">{{ row.leader_name }}</span>
            <el-text v-else type="info">未设置</el-text>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small" effect="light">
              {{ row.is_active ? "是" : "否" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260">
          <template #default="{ row }">
            <el-button size="small" @click="openCreate(row.id)">加子部门</el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="row.parent_id == null"
              @click="removeDept(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="showCreate" title="新建部门" width="420px" destroy-on-close>
      <el-form label-width="90px">
        <el-form-item label="父部门">
          <el-select v-model="form.parent_id" filterable style="width: 100%">
            <el-option v-for="d in deptOptions" :key="d.id" :value="d.id" :label="d.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.kind" clearable placeholder="可空=继承上级" style="width: 100%">
            <el-option value="sales" label="sales" />
            <el-option value="finance" label="finance" />
            <el-option value="supply" label="supply" />
            <el-option value="hr" label="hr" />
            <el-option value="other" label="other" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" @click="createDept">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showEdit" title="编辑部门" width="420px" destroy-on-close>
      <el-form label-width="90px">
        <el-form-item label="父部门">
          <el-select
            v-model="editForm.parent_id"
            filterable
            clearable
            placeholder="空=根部门"
            style="width: 100%"
            :disabled="editForm.parent_id == null && editIsRoot"
          >
            <el-option
              v-for="d in editParentOptions"
              :key="d.id"
              :value="d.id"
              :label="d.label"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="editForm.name" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="editForm.kind" clearable placeholder="可空=继承上级" style="width: 100%">
            <el-option value="sales" label="sales" />
            <el-option value="finance" label="finance" />
            <el-option value="supply" label="supply" />
            <el-option value="hr" label="hr" />
            <el-option value="other" label="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="editForm.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showEdit = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { Plus, Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { ElMessage, ElMessageBox } from "element-plus";
import { buildDeptTree, flatDeptOptions, type DeptItem } from "../utils/deptTree";

const items = ref<DeptItem[]>([]);
const showCreate = ref(false);
const showEdit = ref(false);
const editId = ref(0);
const editIsRoot = ref(false);
const form = reactive<{ parent_id: number | undefined; name: string; kind: string }>({
  parent_id: undefined,
  name: "",
  kind: "",
});
const editForm = reactive<{
  parent_id: number | null | undefined;
  name: string;
  kind: string;
  is_active: boolean;
}>({
  parent_id: undefined,
  name: "",
  kind: "",
  is_active: true,
});

const tree = computed(() => buildDeptTree(items.value));
const deptOptions = computed(() => flatDeptOptions(items.value));
const editParentOptions = computed(() =>
  flatDeptOptions(items.value.filter((d) => d.id !== editId.value))
);

function openCreate(parentId?: number) {
  form.parent_id = parentId ?? deptOptions.value[0]?.id;
  form.name = "";
  form.kind = "";
  showCreate.value = true;
}

function openEdit(row: DeptItem) {
  editId.value = row.id;
  editIsRoot.value = row.parent_id == null;
  editForm.parent_id = row.parent_id;
  editForm.name = row.name;
  editForm.kind = row.kind || "";
  editForm.is_active = row.is_active !== false;
  showEdit.value = true;
}

async function load() {
  const { data } = await http.get("/api/op/org/departments");
  items.value = data.data?.items || [];
  if (form.parent_id == null && deptOptions.value.length) {
    form.parent_id = deptOptions.value[0].id;
  }
}

async function createDept() {
  try {
    await http.post("/api/op/org/departments", {
      parent_id: form.parent_id,
      name: form.name,
      kind: form.kind || null,
    });
    ElMessage.success("已创建");
    showCreate.value = false;
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "失败");
  }
}

async function saveEdit() {
  try {
    const payload: Record<string, any> = {
      name: editForm.name,
      kind: editForm.kind || null,
      is_active: editForm.is_active,
    };
    if (!editIsRoot.value) {
      payload.parent_id = editForm.parent_id ?? null;
    }
    await http.patch(`/api/op/org/departments/${editId.value}`, payload);
    ElMessage.success("已保存");
    showEdit.value = false;
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "失败");
  }
}

async function removeDept(row: DeptItem) {
  try {
    await ElMessageBox.confirm(`确定删除部门「${row.name}」？`, "删除确认", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await http.delete(`/api/op/org/departments/${row.id}`);
    ElMessage.success("已删除");
    await load();
  } catch (e: any) {
    if (e === "cancel" || e === "close") return;
    ElMessage.error(e?.response?.data?.message || "删除失败");
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
