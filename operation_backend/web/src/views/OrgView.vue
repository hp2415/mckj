<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">部门树</h1>
        <p class="page-sub">支持新建 / 编辑 / 删除；有子部门或成员时不可删；管理员可按菜单树配置各部门访问权限</p>
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
        <el-table-column label="类型" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.kind" size="small" effect="plain">{{ kindLabel(row.kind) }}</el-tag>
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
        <el-table-column label="操作" :width="canManagePerms ? 360 : 260">
          <template #default="{ row }">
            <el-button size="small" @click="openCreate(row.id)">加子部门</el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="canManagePerms"
              size="small"
              type="primary"
              plain
              @click="openPerms(row)"
            >
              菜单权限
            </el-button>
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
            <el-option v-for="k in kindOptions" :key="k.value" :value="k.value" :label="k.label" />
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
            <el-option v-for="k in kindOptions" :key="k.value" :value="k.value" :label="k.label" />
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

    <el-drawer
      v-model="permVisible"
      :title="permDept ? `菜单权限 · ${permDept.name}` : '菜单权限'"
      size="620px"
      destroy-on-close
      @closed="onPermDrawerClosed"
    >
      <div v-loading="permLoading" class="perm-drawer-body">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          class="perm-hint"
          title="勾选树与「系统 → 菜单管理」同步；未挂到菜单的业务权限在下方单独配置。未单独配置的子部门会继承上级。超管专属项不可下放。"
        />

        <el-tabs v-model="permTab" :before-leave="beforePermTabLeave" @tab-change="onPermTabChanged">
          <el-tab-pane label="主管" name="manager" />
          <el-tab-pane label="普通用户（预留）" name="staff" />
        </el-tabs>

        <el-alert
          v-if="permTab === 'staff'"
          type="warning"
          :closable="false"
          show-icon
          class="perm-hint"
          title="普通用户暂未开放登录运营后台，此处配置将预留，打开 staff 登录后生效。"
        />

        <el-alert
          v-if="currentRoleMeta && !currentRoleMeta.configured && currentRoleMeta.inherited_from"
          type="success"
          :closable="false"
          show-icon
          class="perm-hint"
          :title="`当前继承自「${currentRoleMeta.inherited_from.name}」，保存后将成为本部门独立配置。`"
        />
        <el-alert
          v-else-if="currentRoleMeta && currentRoleMeta.configured"
          type="info"
          :closable="false"
          show-icon
          class="perm-hint"
          title="本部门已独立配置该角色档菜单。"
        />

        <div class="perm-toolbar">
          <el-button size="small" @click="togglePermExpand">
            {{ permExpanded ? "收起菜单" : "展开菜单" }}
          </el-button>
          <el-button size="small" @click="checkAllMenus">全选菜单</el-button>
          <el-button size="small" @click="clearAllMenus">清空菜单</el-button>
        </div>

        <div class="perm-section-title">侧栏菜单</div>
        <el-scrollbar class="perm-tree-scroll">
          <el-tree
            v-if="menuTree.length"
            ref="permTreeRef"
            :data="menuTree"
            node-key="key"
            show-checkbox
            default-expand-all
            :props="{ label: 'title', children: 'children' }"
            :check-strictly="false"
          >
            <template #default="{ data }">
              <span class="perm-tree-node">
                <el-tag size="small" :type="menuTypeTag(data.menu_type)" effect="plain">
                  {{ menuTypeLabel(data.menu_type) }}
                </el-tag>
                <span class="perm-tree-title">{{ data.title }}</span>
                <code v-if="data.perm_code" class="perm-tree-code">{{ data.perm_code }}</code>
              </span>
            </template>
          </el-tree>
          <el-empty v-else description="暂无菜单，请先在「菜单管理」中配置" :image-size="64" />
        </el-scrollbar>

        <template v-if="extraGroups.length">
          <div class="perm-section-title">其他业务权限</div>
          <div v-for="g in extraGroups" :key="g.group" class="perm-group">
            <div class="perm-group-title">{{ g.label }}</div>
            <el-checkbox-group v-model="extraForm[permTab]">
              <el-checkbox
                v-for="it in g.items"
                :key="it.code"
                :value="it.code"
                :label="it.code"
                border
                class="perm-check"
              >
                {{ it.label }}
              </el-checkbox>
            </el-checkbox-group>
          </div>
        </template>
      </div>
      <template #footer>
        <div class="drawer-footer">
          <el-button @click="permVisible = false">取消</el-button>
          <el-button type="primary" :loading="permSaving" @click="savePerms">保存</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { Plus, Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { ElMessage, ElMessageBox, type TabPaneName } from "element-plus";
import { buildDeptTree, flatDeptOptions, type DeptItem } from "../utils/deptTree";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const canManagePerms = computed(() => auth.has("org.perm.manage"));

const kindOptions = [
  { value: "sales", label: "销售 sales" },
  { value: "finance", label: "财务 finance" },
  { value: "supply", label: "供应链 supply" },
  { value: "hr", label: "人事 hr" },
  { value: "ops_assistant", label: "运营助理 ops_assistant" },
  { value: "other", label: "其他 other" },
];

function kindLabel(k: string) {
  return kindOptions.find((x) => x.value === k)?.label || k;
}

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

type MenuPermNode = {
  key: string;
  id: number;
  title: string;
  menu_type: string;
  perm_code?: string | null;
  path?: string;
  children?: MenuPermNode[];
};

type ExtraGroup = {
  group: string;
  label: string;
  items: { code: string; label: string }[];
};

const permVisible = ref(false);
const permLoading = ref(false);
const permSaving = ref(false);
const permDept = ref<DeptItem | null>(null);
const permTab = ref<"manager" | "staff">("manager");
const menuTree = ref<MenuPermNode[]>([]);
const extraGroups = ref<ExtraGroup[]>([]);
const permMeta = ref<{ manager: any; staff: any } | null>(null);
const permTreeRef = ref();
const permExpanded = ref(true);
/** 各角色档：菜单树选中的权限码 + 其他业务权限 */
const menuCodes = reactive<{ manager: string[]; staff: string[] }>({
  manager: [],
  staff: [],
});
const extraForm = reactive<{ manager: string[]; staff: string[] }>({
  manager: [],
  staff: [],
});

const currentRoleMeta = computed(() => {
  if (!permMeta.value) return null;
  return permTab.value === "manager" ? permMeta.value.manager : permMeta.value.staff;
});

const extraCodeSet = computed(() => {
  const s = new Set<string>();
  for (const g of extraGroups.value) {
    for (const it of g.items || []) {
      if (it.code) s.add(it.code);
    }
  }
  return s;
});

function menuTypeLabel(t: string) {
  return ({ directory: "目录", menu: "菜单", button: "按钮" } as Record<string, string>)[t] || t;
}

function menuTypeTag(t: string): "info" | "primary" | "danger" {
  if (t === "directory") return "info";
  if (t === "button") return "danger";
  return "primary";
}

function collectKeysByCodes(nodes: MenuPermNode[], codes: Set<string>): string[] {
  const keys: string[] = [];
  const walk = (list: MenuPermNode[]) => {
    for (const n of list) {
      if (n.perm_code && codes.has(n.perm_code)) keys.push(n.key);
      if (n.children?.length) walk(n.children);
    }
  };
  walk(nodes);
  return keys;
}

function collectAllMenuKeys(nodes: MenuPermNode[]): string[] {
  const keys: string[] = [];
  const walk = (list: MenuPermNode[]) => {
    for (const n of list) {
      keys.push(n.key);
      if (n.children?.length) walk(n.children);
    }
  };
  walk(nodes);
  return keys;
}

function readTreeCodes(): string[] {
  const nodes: MenuPermNode[] = permTreeRef.value?.getCheckedNodes?.(false) || [];
  const codes = new Set<string>();
  for (const n of nodes) {
    if (n.perm_code) codes.add(n.perm_code);
  }
  return [...codes];
}

function applyTreeFromCodes(codes: string[]) {
  const set = new Set(codes);
  const keys = collectKeysByCodes(menuTree.value, set);
  nextTick(() => {
    permTreeRef.value?.setCheckedKeys?.(keys, false);
  });
}

function syncTabUiFromStore() {
  const codes = menuCodes[permTab.value] || [];
  applyTreeFromCodes(codes);
}

function snapshotCurrentTabTree() {
  menuCodes[permTab.value] = readTreeCodes();
}

function beforePermTabLeave(_active: TabPaneName, oldActive: TabPaneName) {
  const role = (oldActive || permTab.value) as "manager" | "staff";
  menuCodes[role] = readTreeCodes();
  return true;
}

async function onPermTabChanged(name: TabPaneName) {
  await nextTick();
  const role = (name || permTab.value) as "manager" | "staff";
  applyTreeFromCodes(menuCodes[role] || []);
}

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

function splitCodes(codes: string[]) {
  const menu: string[] = [];
  const extra: string[] = [];
  for (const c of codes || []) {
    if (extraCodeSet.value.has(c)) extra.push(c);
    else menu.push(c);
  }
  return { menu, extra };
}

async function openPerms(row: DeptItem) {
  permDept.value = row;
  permTab.value = "manager";
  permVisible.value = true;
  permLoading.value = true;
  try {
    const cat = await http.get("/api/op/org/dept-perms/catalog");
    if (cat.data.code !== 200) throw new Error(cat.data.message);
    menuTree.value = cat.data.data?.menu_tree || [];
    extraGroups.value = cat.data.data?.extra_groups || [];

    const { data } = await http.get(`/api/op/org/dept-perms/departments/${row.id}`);
    if (data.code !== 200) throw new Error(data.message);
    permMeta.value = {
      manager: data.data.manager,
      staff: data.data.staff,
    };
    const m = splitCodes(data.data.manager?.codes || []);
    const s = splitCodes(data.data.staff?.codes || []);
    menuCodes.manager = m.menu;
    menuCodes.staff = s.menu;
    extraForm.manager = m.extra;
    extraForm.staff = s.extra;
    await nextTick();
    applyTreeFromCodes(menuCodes.manager);
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "加载权限失败");
    permVisible.value = false;
  } finally {
    permLoading.value = false;
  }
}

function onPermDrawerClosed() {
  menuTree.value = [];
  permTreeRef.value = undefined;
}

function togglePermExpand() {
  permExpanded.value = !permExpanded.value;
  const keys = collectAllMenuKeys(menuTree.value);
  nextTick(() => {
    for (const k of keys) {
      const node = permTreeRef.value?.store?.nodesMap?.[k];
      if (node && node.childNodes?.length) {
        node.expanded = permExpanded.value;
      }
    }
  });
}

function checkAllMenus() {
  const keys = collectAllMenuKeys(menuTree.value);
  permTreeRef.value?.setCheckedKeys?.(keys, false);
}

function clearAllMenus() {
  permTreeRef.value?.setCheckedKeys?.([], false);
}

async function savePerms() {
  if (!permDept.value) return;
  snapshotCurrentTabTree();
  permSaving.value = true;
  try {
    const manager = [...new Set([...menuCodes.manager, ...extraForm.manager])];
    const staff = [...new Set([...menuCodes.staff, ...extraForm.staff])];
    const { data } = await http.put(`/api/op/org/dept-perms/departments/${permDept.value.id}`, {
      manager,
      staff,
    });
    if (data.code !== 200) throw new Error(data.message);
    ElMessage.success(data.message || "已保存");
    permMeta.value = {
      manager: data.data.manager,
      staff: data.data.staff,
    };
    const m = splitCodes(data.data.manager?.codes || []);
    const s = splitCodes(data.data.staff?.codes || []);
    menuCodes.manager = m.menu;
    menuCodes.staff = s.menu;
    extraForm.manager = m.extra;
    extraForm.staff = s.extra;
    applyTreeFromCodes(menuCodes[permTab.value]);
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "保存失败");
  } finally {
    permSaving.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
}

.perm-drawer-body {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.perm-hint {
  margin-bottom: 0.85rem;
}

.perm-toolbar {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.perm-section-title {
  font-weight: 650;
  margin: 0.35rem 0 0.55rem;
  color: var(--op-ink);
}

.perm-tree-scroll {
  height: 360px;
  border: 1px solid var(--op-card-border, #e2e8f0);
  border-radius: 8px;
  padding: 0.5rem 0.65rem;
  margin-bottom: 1rem;
  background: var(--op-card, #fff);
}

.perm-tree-node {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding-right: 0.5rem;
}

.perm-tree-title {
  font-size: 0.92rem;
}

.perm-tree-code {
  font-size: 11px;
  color: var(--el-color-primary);
  opacity: 0.85;
}

.perm-group {
  margin-bottom: 1.1rem;
}

.perm-group-title {
  font-weight: 650;
  margin-bottom: 0.55rem;
  color: var(--op-ink);
}

.perm-check {
  margin: 0 0.45rem 0.45rem 0 !important;
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}
</style>
