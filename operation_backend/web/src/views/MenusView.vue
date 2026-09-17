<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">菜单管理</h1>
        <p class="page-sub">维护侧栏目录 / 菜单 / 按钮；侧栏按权限与启用状态动态展示</p>
      </div>
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate()">添加菜单</el-button>
        <el-button @click="toggleExpand">{{ expanded ? "收起" : "展开" }}</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card filter-card" shadow="never">
      <el-form :inline="true" @submit.prevent="applySearch">
        <el-form-item label="菜单名称">
          <el-input v-model="filters.title" clearable placeholder="名称" style="width: 180px" />
        </el-form-item>
        <el-form-item label="路由地址">
          <el-input v-model="filters.path" clearable placeholder="如 /dashboard" style="width: 180px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="applySearch">搜索</el-button>
          <el-button @click="resetSearch">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="soft-card" shadow="never">
      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="filteredTree"
        row-key="id"
        :tree-props="{ children: 'children' }"
        :default-expand-all="false"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
      >
        <el-table-column prop="title" label="菜单名称" min-width="180" />
        <el-table-column label="菜单类型" width="100">
          <template #default="{ row }">
            <el-tag :type="typeTag(row.menu_type)" size="small" effect="light">
              {{ typeLabel(row.menu_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="路由" min-width="140">
          <template #default="{ row }">
            <span v-if="row.menu_type !== 'button'">{{ row.link || row.path || "—" }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="权限标识" min-width="160">
          <template #default="{ row }">
            <code v-if="row.perm_code" class="perm-code">{{ row.perm_code }}</code>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="sort_order" label="排序" width="80" />
        <el-table-column label="图标" width="90">
          <template #default="{ row }">
            <el-icon v-if="row.icon" :size="18"><component :is="resolveMenuIcon(row.icon)" /></el-icon>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_enable ? 'success' : 'info'" size="small" effect="light">
              {{ row.is_enable ? "启用" : "停用" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ row.updated_at || "—" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.menu_type !== 'button'"
              size="small"
              text
              type="primary"
              @click="openCreate(row)"
            >
              新增
            </el-button>
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="removeRow(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogTitle"
      width="720px"
      align-center
      destroy-on-close
      class="menu-dialog"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="菜单类型" prop="menu_type">
          <el-radio-group v-model="form.menu_type" :disabled="!!editId">
            <el-radio-button value="directory">目录</el-radio-button>
            <el-radio-button value="menu">菜单</el-radio-button>
            <el-radio-button value="button">按钮</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="上级菜单">
              <el-tree-select
                v-model="form.parent_id"
                :data="parentOptions"
                clearable
                check-strictly
                filterable
                :render-after-expand="false"
                placeholder="空=顶级"
                style="width: 100%"
                :props="{ label: 'title', value: 'id', children: 'children' }"
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="菜单名称" prop="title">
              <el-input v-model="form.title" maxlength="100" placeholder="显示名称" />
            </el-form-item>
          </el-col>
        </el-row>

        <template v-if="form.menu_type !== 'button'">
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="路由地址" :prop="form.menu_type === 'menu' ? 'path' : undefined">
                <el-input v-model="form.path" placeholder="如 /dashboard" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="组件路径">
                <el-input v-model="form.component" placeholder="如 CampaignsView（备注用）" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="图标">
                <el-select v-model="form.icon" clearable filterable placeholder="Element Plus 图标名" style="width: 100%">
                  <el-option v-for="name in MENU_ICON_OPTIONS" :key="name" :label="name" :value="name">
                    <span class="icon-opt">
                      <el-icon><component :is="resolveMenuIcon(name)" /></el-icon>
                      {{ name }}
                    </span>
                  </el-option>
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="权限标识">
                <el-input v-model="form.perm_code" placeholder="如 usage.dashboard.view" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="外部链接">
                <el-input v-model="form.link" placeholder="https://..." />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="排序">
                <el-input-number v-model="form.sort_order" :min="0" controls-position="right" style="width: 100%" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="6">
              <el-form-item label="启用">
                <el-switch v-model="form.is_enable" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="隐藏菜单">
                <el-switch v-model="form.is_hide" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="内嵌">
                <el-switch v-model="form.is_iframe" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="页面缓存">
                <el-switch v-model="form.keep_alive" />
              </el-form-item>
            </el-col>
          </el-row>
        </template>

        <template v-else>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="权限标识" prop="perm_code">
                <el-input v-model="form.perm_code" placeholder="如 activity.campaign.edit" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="排序">
                <el-input-number v-model="form.sort_order" :min="0" controls-position="right" style="width: 100%" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="启用">
            <el-switch v-model="form.is_enable" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import { Plus, Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { MENU_ICON_OPTIONS, resolveMenuIcon } from "../utils/menuIcons";

type MenuNode = {
  id: number;
  parent_id: number | null;
  menu_type: "directory" | "menu" | "button";
  title: string;
  path: string;
  component: string;
  icon: string;
  perm_code: string;
  sort_order: number;
  is_enable: boolean;
  is_hide: boolean;
  link: string;
  is_iframe: boolean;
  keep_alive: boolean;
  updated_at?: string | null;
  children?: MenuNode[];
};

const loading = ref(false);
const saving = ref(false);
const tree = ref<MenuNode[]>([]);
const tableRef = ref();
const expanded = ref(false);
const dialogVisible = ref(false);
const editId = ref<number | null>(null);
const formRef = ref<FormInstance>();

const filters = reactive({ title: "", path: "" });
const applied = reactive({ title: "", path: "" });

const emptyForm = () => ({
  parent_id: null as number | null,
  menu_type: "menu" as MenuNode["menu_type"],
  title: "",
  path: "",
  component: "",
  icon: "",
  perm_code: "",
  sort_order: 10,
  is_enable: true,
  is_hide: false,
  link: "",
  is_iframe: false,
  keep_alive: true,
});

const form = reactive(emptyForm());

const rules = computed<FormRules>(() => ({
  title: [{ required: true, message: "请输入菜单名称", trigger: "blur" }],
  menu_type: [{ required: true, message: "请选择类型", trigger: "change" }],
  path:
    form.menu_type === "menu"
      ? [{ required: true, message: "请输入路由地址", trigger: "blur" }]
      : [],
  perm_code:
    form.menu_type === "button"
      ? [{ required: true, message: "请输入权限标识", trigger: "blur" }]
      : [],
}));

const dialogTitle = computed(() => {
  if (editId.value) return form.menu_type === "button" ? "编辑按钮" : "编辑菜单";
  return form.menu_type === "button" ? "新增按钮" : "新增菜单";
});

function typeLabel(t: string) {
  return ({ directory: "目录", menu: "菜单", button: "按钮" } as Record<string, string>)[t] || t;
}

function typeTag(t: string): "info" | "primary" | "danger" {
  if (t === "directory") return "info";
  if (t === "button") return "danger";
  return "primary";
}

function cloneTree(nodes: MenuNode[]): MenuNode[] {
  return nodes.map((n) => ({
    ...n,
    children: n.children?.length ? cloneTree(n.children) : [],
  }));
}

function filterTree(nodes: MenuNode[]): MenuNode[] {
  const titleQ = applied.title.trim().toLowerCase();
  const pathQ = applied.path.trim().toLowerCase();
  if (!titleQ && !pathQ) return nodes;

  const out: MenuNode[] = [];
  for (const n of nodes) {
    const kids = n.children?.length ? filterTree(n.children) : [];
    const titleOk = !titleQ || n.title.toLowerCase().includes(titleQ);
    const pathOk = !pathQ || (n.path || "").toLowerCase().includes(pathQ) || (n.link || "").toLowerCase().includes(pathQ);
    if (kids.length) {
      out.push({ ...n, children: kids });
    } else if (titleOk && pathOk) {
      out.push({ ...n, children: [] });
    }
  }
  return out;
}

const filteredTree = computed(() => filterTree(cloneTree(tree.value)));

function stripButtonsForParent(nodes: MenuNode[], excludeId: number | null): MenuNode[] {
  return nodes
    .filter((n) => n.menu_type !== "button" && n.id !== excludeId)
    .map((n) => ({
      ...n,
      children: n.children?.length ? stripButtonsForParent(n.children, excludeId) : [],
    }));
}

const parentOptions = computed(() => stripButtonsForParent(tree.value, editId.value));

async function load() {
  loading.value = true;
  try {
    const { data } = await http.get("/api/op/menus/tree");
    if (data.code !== 200) throw new Error(data.message || "加载失败");
    tree.value = data.data || [];
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "加载失败");
  } finally {
    loading.value = false;
  }
}

function applySearch() {
  applied.title = filters.title;
  applied.path = filters.path;
}

function resetSearch() {
  filters.title = "";
  filters.path = "";
  applied.title = "";
  applied.path = "";
}

function openCreate(parent?: MenuNode) {
  editId.value = null;
  Object.assign(form, emptyForm());
  if (parent) {
    form.parent_id = parent.id;
    form.menu_type = parent.menu_type === "menu" ? "button" : "menu";
  }
  dialogVisible.value = true;
}

function openEdit(row: MenuNode) {
  editId.value = row.id;
  Object.assign(form, {
    parent_id: row.parent_id,
    menu_type: row.menu_type,
    title: row.title,
    path: row.path || "",
    component: row.component || "",
    icon: row.icon || "",
    perm_code: row.perm_code || "",
    sort_order: row.sort_order ?? 0,
    is_enable: !!row.is_enable,
    is_hide: !!row.is_hide,
    link: row.link || "",
    is_iframe: !!row.is_iframe,
    keep_alive: row.keep_alive !== false,
  });
  dialogVisible.value = true;
}

async function submit() {
  await formRef.value?.validate().catch(() => Promise.reject());
  saving.value = true;
  const payload = {
    parent_id: form.parent_id,
    menu_type: form.menu_type,
    title: form.title.trim(),
    path: form.path || null,
    component: form.component || null,
    icon: form.icon || null,
    perm_code: form.perm_code || null,
    sort_order: form.sort_order,
    is_enable: form.is_enable,
    is_hide: form.is_hide,
    link: form.link || null,
    is_iframe: form.is_iframe,
    keep_alive: form.keep_alive,
    clear_parent: form.parent_id == null,
  };
  try {
    if (editId.value) {
      const { data } = await http.put(`/api/op/menus/${editId.value}`, payload);
      if (data.code !== 200) throw new Error(data.message || "保存失败");
      ElMessage.success("已更新");
    } else {
      const { data } = await http.post("/api/op/menus", payload);
      if (data.code !== 200) throw new Error(data.message || "创建失败");
      ElMessage.success("已创建");
    }
    dialogVisible.value = false;
    await load();
    window.dispatchEvent(new Event("op-menus-changed"));
  } catch (e: any) {
    if (e !== false) {
      ElMessage.error(e?.response?.data?.message || e?.message || "保存失败");
    }
  } finally {
    saving.value = false;
  }
}

async function removeRow(row: MenuNode) {
  const hasKids = !!(row.children && row.children.length);
  try {
    await ElMessageBox.confirm(
      hasKids
        ? `「${row.title}」及其子菜单将一并删除，确定继续？`
        : `确定删除「${row.title}」？`,
      "提示",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
    const { data } = await http.delete(`/api/op/menus/${row.id}`);
    if (data.code !== 200) throw new Error(data.message || "删除失败");
    ElMessage.success("已删除");
    await load();
    window.dispatchEvent(new Event("op-menus-changed"));
  } catch (e: any) {
    if (e !== "cancel") {
      ElMessage.error(e?.response?.data?.message || e?.message || "删除失败");
    }
  }
}

function toggleExpand() {
  expanded.value = !expanded.value;
  nextTick(() => {
    const walk = (rows: MenuNode[]) => {
      rows.forEach((row) => {
        if (row.children?.length) {
          tableRef.value?.toggleRowExpansion(row, expanded.value);
          walk(row.children);
        }
      });
    };
    walk(filteredTree.value);
  });
}

onMounted(load);
</script>

<style scoped>
.filter-card {
  margin-bottom: 12px;
}
.muted {
  color: var(--el-text-color-placeholder);
}
.perm-code {
  font-size: 12px;
  color: var(--el-color-primary);
}
.icon-opt {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
</style>
