<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">商品规格</h1>
        <p class="page-sub">查询商品、维护规格 ID / 成本价与上架状态；绑定规格 ID 后可同步主系统成本价</p>
      </div>
      <div class="toolbar">
        <el-button
          v-if="canEdit"
          type="primary"
          :disabled="!selectedWithSp.length"
          :loading="syncing"
          @click="syncSelected"
        >
          同步所选成本
        </el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card filter-card" shadow="never">
      <el-form :inline="true" class="filters" @submit.prevent="onSearch">
        <el-form-item label="关键词">
          <el-input
            v-model="filters.keyword"
            clearable
            placeholder="名称 / 商品号 / 规格 ID"
            style="width: 220px"
            @keyup.enter="onSearch"
          />
        </el-form-item>
        <el-form-item label="供应商">
          <el-input
            v-model="filters.supplier_name"
            clearable
            placeholder="渠道商"
            style="width: 160px"
            @keyup.enter="onSearch"
          />
        </el-form-item>
        <el-form-item label="上架">
          <el-select v-model="filters.is_active" clearable placeholder="全部" style="width: 110px">
            <el-option label="上架" value="1" />
            <el-option label="已下架" value="0" />
          </el-select>
        </el-form-item>
        <el-form-item label="规格 ID">
          <el-select v-model="filters.has_sp_id" clearable placeholder="全部" style="width: 120px">
            <el-option label="有规格 ID" value="1" />
            <el-option label="无规格 ID" value="0" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onSearch">搜索</el-button>
          <el-button @click="onReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="soft-card" shadow="never">
      <el-table
        v-loading="loading"
        :data="items"
        stripe
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
        @selection-change="onSelectionChange"
      >
        <el-table-column v-if="canEdit" type="selection" width="48" />
        <el-table-column label="图片" width="72">
          <template #default="{ row }">
            <el-image
              v-if="row.cover_img"
              class="thumb"
              :src="row.cover_img"
              :preview-src-list="[row.cover_img]"
              preview-teleported
              fit="cover"
              lazy
            />
            <div v-else class="thumb-placeholder">无图</div>
          </template>
        </el-table-column>
        <el-table-column label="商品" min-width="200" align="left" header-align="left">
          <template #default="{ row }">
            <div class="name-cell">
              <el-text tag="b" truncated class="name-text">{{ row.product_name }}</el-text>
              <span class="muted">#{{ row.product_id }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="规格 ID" min-width="150">
          <template #default="{ row }">
            <code v-if="row.mibuddy_sp_id" class="sp-code">{{ row.mibuddy_sp_id }}</code>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="售价" width="100">
          <template #default="{ row }">¥{{ formatMoney(row.price) }}</template>
        </el-table-column>
        <el-table-column label="成本价" width="100">
          <template #default="{ row }">
            <span v-if="row.cost_price != null">¥{{ formatMoney(row.cost_price) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="supplier_name" label="供应商" min-width="120">
          <template #default="{ row }">{{ row.supplier_name || "—" }}</template>
        </el-table-column>
        <el-table-column label="上架" width="100">
          <template #default="{ row }">
            <el-switch
              v-if="canEdit"
              :model-value="row.is_active"
              :loading="togglingId === row.id"
              @change="(v: boolean | string | number) => toggleActive(row, Boolean(v))"
            />
            <el-tag v-else size="small" :type="row.is_active ? 'success' : 'info'" effect="plain">
              {{ row.is_active ? "上架" : "已下架" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">{{ canEdit ? "编辑" : "查看" }}</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="canEdit ? '编辑商品规格' : '商品详情'"
      width="480px"
      destroy-on-close
    >
      <div v-if="editing" class="edit-head">
        <el-image
          v-if="editing.cover_img"
          class="edit-thumb"
          :src="editing.cover_img"
          fit="cover"
        />
        <div>
          <div class="edit-name">{{ editing.product_name }}</div>
          <div class="muted">平台号 {{ editing.product_id }} · 售价 ¥{{ formatMoney(editing.price) }}</div>
        </div>
      </div>
      <el-form label-width="100px" class="edit-form">
        <el-form-item label="规格 ID">
          <el-input
            v-model="form.mibuddy_sp_id"
            :disabled="!canEdit"
            clearable
            placeholder="主系统规格 ID，空表示未绑定"
          />
        </el-form-item>
        <el-form-item label="成本价">
          <el-input-number
            v-model="form.cost_price"
            :disabled="!canEdit"
            :min="0"
            :precision="2"
            :step="0.01"
            controls-position="right"
            style="width: 100%"
          />
          <div class="hint">有规格 ID 时保存后会自动同步主系统成本；无 ID 可手填兜底。</div>
        </el-form-item>
        <el-form-item label="上架状态">
          <el-switch v-model="form.is_active" :disabled="!canEdit" active-text="上架" inactive-text="下架" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ canEdit ? "取消" : "关闭" }}</el-button>
        <el-button v-if="canEdit" type="primary" :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { Refresh } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import http from "../api/http";
import { useAuthStore } from "../stores/auth";

type ProductRow = {
  id: number;
  product_id: string;
  product_name: string;
  price: number;
  cost_price: number | null;
  mibuddy_sp_id: string | null;
  is_active: boolean;
  cover_img: string | null;
  supplier_name: string | null;
};

const auth = useAuthStore();
const canEdit = computed(() => auth.has("product.spec.edit"));

const loading = ref(false);
const syncing = ref(false);
const saving = ref(false);
const togglingId = ref<number | null>(null);
const items = ref<ProductRow[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const selected = ref<ProductRow[]>([]);

const filters = reactive({
  keyword: "",
  supplier_name: "",
  is_active: "" as string,
  has_sp_id: "" as string,
});

const dialogVisible = ref(false);
const editing = ref<ProductRow | null>(null);
const form = reactive({
  mibuddy_sp_id: "",
  cost_price: null as number | null,
  is_active: true,
});

const selectedWithSp = computed(() =>
  selected.value.filter((r) => !!(r.mibuddy_sp_id || "").trim())
);

function formatMoney(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(2);
}

function onSelectionChange(rows: ProductRow[]) {
  selected.value = rows;
}

function onSearch() {
  page.value = 1;
  load();
}

function onReset() {
  filters.keyword = "";
  filters.supplier_name = "";
  filters.is_active = "";
  filters.has_sp_id = "";
  page.value = 1;
  load();
}

function onSizeChange() {
  page.value = 1;
  load();
}

async function load() {
  loading.value = true;
  try {
    const { data } = await http.get("/api/op/products", {
      params: {
        keyword: filters.keyword || undefined,
        supplier_name: filters.supplier_name || undefined,
        is_active: filters.is_active || undefined,
        has_sp_id: filters.has_sp_id || undefined,
        skip: (page.value - 1) * pageSize.value,
        limit: pageSize.value,
      },
    });
    if (data.code !== 200) throw new Error(data.message || "加载失败");
    items.value = data.data?.items || [];
    total.value = data.data?.total || 0;
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "加载失败");
  } finally {
    loading.value = false;
  }
}

function openEdit(row: ProductRow) {
  editing.value = row;
  form.mibuddy_sp_id = row.mibuddy_sp_id || "";
  form.cost_price = row.cost_price != null ? Number(row.cost_price) : null;
  form.is_active = !!row.is_active;
  dialogVisible.value = true;
}

async function saveEdit() {
  if (!editing.value || !canEdit.value) return;
  saving.value = true;
  try {
    const sp = (form.mibuddy_sp_id || "").trim();
    const payload: Record<string, unknown> = {
      is_active: form.is_active,
    };
    if (!sp) {
      payload.clear_sp_id = true;
    } else {
      payload.mibuddy_sp_id = sp;
    }
    if (form.cost_price == null) {
      payload.clear_cost_price = true;
    } else {
      payload.cost_price = form.cost_price;
    }
    const { data } = await http.patch(`/api/op/products/${editing.value.id}`, payload);
    if (data.code !== 200) throw new Error(data.message || "保存失败");
    const missing = data.data?.sync_missing_lines || [];
    if (data.data?.sp_changed && data.data?.sync) {
      const s = data.data.sync;
      if (missing.length) {
        ElMessage.warning(`已保存；成本未命中：${missing[0]}`);
      } else if (s.failed_batches) {
        ElMessage.warning(`已保存；成本同步部分失败（${(s.errors || [])[0] || "请检查配置"}）`);
      } else {
        ElMessage.success(`已保存并同步成本（更新 ${s.updated || 0} 条）`);
      }
    } else {
      ElMessage.success("已保存");
    }
    dialogVisible.value = false;
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "保存失败");
  } finally {
    saving.value = false;
  }
}

async function toggleActive(row: ProductRow, next: boolean) {
  if (!canEdit.value) return;
  togglingId.value = row.id;
  try {
    const { data } = await http.patch(`/api/op/products/${row.id}`, { is_active: next });
    if (data.code !== 200) throw new Error(data.message || "更新失败");
    row.is_active = next;
    ElMessage.success(next ? "已上架" : "已下架");
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "更新失败");
  } finally {
    togglingId.value = null;
  }
}

async function syncSelected() {
  if (!selectedWithSp.value.length) return;
  syncing.value = true;
  try {
    const { data } = await http.post("/api/op/products/sync-cost", {
      product_ids: selectedWithSp.value.map((r) => r.id),
    });
    if (data.code !== 200) throw new Error(data.message || "同步失败");
    const s = data.data || {};
    const missing = s.missing_lines || [];
    if (s.failed_batches) {
      ElMessage.warning(`同步部分失败：${(s.errors || [])[0] || "请检查 MiBuddy 配置"}`);
    } else if (missing.length) {
      ElMessage.warning(`已更新 ${s.updated || 0} 条；未命中：${missing[0]}`);
    } else {
      ElMessage.success(`同步完成：查询 ${s.queried || 0} 个规格，更新 ${s.updated || 0} 条`);
    }
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "同步失败");
  } finally {
    syncing.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
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

.thumb {
  width: 48px;
  height: 48px;
  border-radius: 6px;
  display: block;
}

.thumb-placeholder {
  width: 48px;
  height: 48px;
  border-radius: 6px;
  background: var(--op-hover, #f1f5f9);
  color: var(--op-muted, #94a3b8);
  font-size: 0.7rem;
  display: flex;
  align-items: center;
  justify-content: center;
}

.name-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.15rem;
  min-width: 0;
  width: 100%;
  text-align: left;
}

.name-text {
  width: 100%;
  text-align: left;
}

.muted {
  color: var(--op-muted, #94a3b8);
  font-size: 0.8rem;
}

.sp-code {
  font-size: 0.8rem;
  background: var(--op-hover, #f1f5f9);
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
}

.pager {
  margin-top: 1rem;
  display: flex;
  justify-content: flex-end;
}

.edit-head {
  display: flex;
  gap: 0.85rem;
  align-items: center;
  margin-bottom: 1rem;
  padding-bottom: 0.85rem;
  border-bottom: 1px solid var(--op-border, #e2e8f0);
}

.edit-thumb {
  width: 56px;
  height: 56px;
  border-radius: 8px;
  flex-shrink: 0;
}

.edit-name {
  font-weight: 600;
  line-height: 1.35;
}

.edit-form .hint {
  margin-top: 0.35rem;
  font-size: 0.75rem;
  color: var(--op-muted, #94a3b8);
  line-height: 1.4;
}
</style>
