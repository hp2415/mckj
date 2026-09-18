<template>
  <div class="camp-page">
    <div class="page-head">
      <div>
        <h1 class="page-title">活动管理</h1>
        <p class="page-sub">维护营销活动窗口、受众与海报 · 与桌面端共用同一套数据</p>
      </div>
      <div class="toolbar">
        <el-button :icon="Refresh" :loading="loading" @click="reload">刷新</el-button>
        <el-button
          v-if="canEdit"
          type="primary"
          :icon="Plus"
          @click="openCreate"
        >
          新建活动
        </el-button>
      </div>
    </div>

    <el-row :gutter="16" class="stats-row">
      <el-col :xs="12" :sm="6" v-for="s in statCards" :key="s.key">
        <div
          class="art-stat-card"
          :class="{ active: filterEffective === s.key }"
          :style="{ '--accent': s.color }"
          @click="toggleEffective(s.key)"
        >
          <div class="art-stat-main">
            <span class="art-stat-label">{{ s.label }}</span>
            <div class="art-stat-val">{{ stats[s.key] ?? 0 }}</div>
            <span class="art-stat-hint">{{ filterEffective === s.key ? "点击取消筛选" : "点击筛选" }}</span>
          </div>
          <div class="art-stat-icon">
            <el-icon :size="22"><component :is="s.icon" /></el-icon>
          </div>
        </div>
      </el-col>
    </el-row>

    <el-card class="soft-card filter-card" shadow="never">
      <div class="filters">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索活动名称"
          :prefix-icon="Search"
          style="width: 220px"
          @keyup.enter="loadList"
          @clear="loadList"
        />
        <el-radio-group v-model="filterStatus" @change="loadList">
          <el-radio-button value="">全部开关</el-radio-button>
          <el-radio-button value="enabled">已开启</el-radio-button>
          <el-radio-button value="disabled">已关闭</el-radio-button>
        </el-radio-group>
        <el-button type="primary" plain :icon="Search" @click="loadList">查询</el-button>
      </div>
    </el-card>

    <div v-loading="loading" class="camp-grid">
      <el-empty v-if="!loading && !items.length" description="暂无活动，点击右上角新建" />
      <div v-for="item in items" :key="item.id" class="camp-card soft-card">
        <div class="camp-cover">
          <el-image
            v-if="item.cover_url || item.cover_path"
            :src="item.cover_url || item.cover_path"
            fit="cover"
            lazy
            class="cover-img"
            :preview-src-list="[item.cover_url || item.cover_path]"
            preview-teleported
          />
          <div v-else class="cover-placeholder">
            <el-icon :size="36"><Picture /></el-icon>
            <span>暂无海报</span>
          </div>
          <el-tag class="eff-tag" :type="effTagType(item.effective_status)" effect="dark" round>
            {{ item.effective_label }}
          </el-tag>
        </div>
        <div class="camp-body">
          <div class="camp-title-row">
            <h3 class="camp-name" :title="item.name">{{ item.name }}</h3>
            <el-switch
              v-if="canEdit"
              :model-value="item.status === 'enabled'"
              inline-prompt
              active-text="开"
              inactive-text="关"
              @change="onSwitchChange(item, $event)"
            />
            <el-tag v-else size="small" :type="item.status === 'enabled' ? 'success' : 'info'">
              {{ item.status === "enabled" ? "开启" : "关闭" }}
            </el-tag>
          </div>
          <div class="meta-line">
            <el-icon><Calendar /></el-icon>
            <span>{{ fmtRange(item.start_at, item.end_at) }}</span>
          </div>
          <div class="meta-line">
            <el-icon><UserFilled /></el-icon>
            <span class="audience">{{ item.audience_label }}</span>
          </div>
          <div class="chip-row">
            <el-tag size="small" effect="plain">优先级 {{ item.priority }}</el-tag>
            <el-tag size="small" effect="plain" type="warning">
              海报 {{ item.active_poster_count }}/{{ item.poster_count }}
            </el-tag>
          </div>
          <p class="rules-preview">{{ item.rules || "未填写活动规则" }}</p>
          <div class="camp-actions">
            <el-button size="small" :icon="Picture" @click="openPosters(item)">海报</el-button>
            <el-button v-if="canEdit" size="small" :icon="EditPen" @click="openEdit(item)">编辑</el-button>
            <el-popconfirm
              v-if="canEdit"
              title="确定删除该活动及其海报？"
              confirm-button-text="删除"
              cancel-button-text="取消"
              @confirm="removeCamp(item)"
            >
              <template #reference>
                <el-button size="small" type="danger" plain :icon="Delete">删除</el-button>
              </template>
            </el-popconfirm>
          </div>
        </div>
      </div>
    </div>

    <!-- 新建 / 编辑 -->
    <el-drawer
      v-model="formVisible"
      :title="formMode === 'create' ? '新建活动' : '编辑活动'"
      size="520px"
      destroy-on-close
    >
      <el-form label-position="top" class="camp-form">
        <el-form-item label="活动名称" required>
          <el-input v-model="form.name" maxlength="120" show-word-limit placeholder="例如：三月开学季" />
        </el-form-item>
        <el-form-item label="活动时间" required>
          <el-date-picker
            v-model="form.range"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始"
            end-placeholder="结束"
            format="YYYY-MM-DD HH:mm"
            value-format="YYYY-MM-DD HH:mm:ss"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="面向客户类型" required>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="form-hint"
            title="勾选「通用」则所有客户可见；有专项活动时不会再推通用场。"
          />
          <el-checkbox-group v-model="form.audience_unit_types" class="audience-group">
            <el-checkbox
              v-for="c in audienceChoices"
              :key="c"
              :value="c"
              :label="c"
              border
            />
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="活动规则">
          <el-input
            v-model="form.rules"
            type="textarea"
            :rows="8"
            placeholder="给 AI / 销售的玩法说明（力度、门槛、禁说口径）。不要当微信原文整段发出。"
          />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="活动状态">
              <el-segmented
                v-model="form.status"
                :options="[
                  { label: '开启', value: 'enabled' },
                  { label: '关闭', value: 'disabled' },
                ]"
                block
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="优先级（越大越优先）">
              <el-input-number v-model="form.priority" :min="-100" :max="1000" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <div class="drawer-footer">
          <el-button @click="formVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="saveForm">保存</el-button>
        </div>
      </template>
    </el-drawer>

    <!-- 海报管理 -->
    <el-drawer
      v-model="posterVisible"
      :title="posterCamp ? `海报 · ${posterCamp.name}` : '海报管理'"
      size="720px"
      destroy-on-close
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="form-hint"
        title="图片保存在服务器 media/campaigns/{id}/。外发时优先发给该客户还没收过的图；都发过则从最早那张开始轮询。"
      />

      <el-upload
        v-if="canEdit"
        class="poster-upload"
        drag
        multiple
        :show-file-list="false"
        accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
        :http-request="doUpload"
        :disabled="uploading"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖拽图片到此处，或<em>点击上传</em></div>
        <template #tip>
          <div class="el-upload__tip">支持 JPG / PNG / WEBP，单张不超过 8MB，可多选</div>
        </template>
      </el-upload>

      <div v-loading="posterLoading" class="poster-grid">
        <el-empty v-if="!posterLoading && !posters.length" description="尚未上传海报" />
        <div v-for="(p, idx) in posters" :key="p.id" class="poster-item soft-card">
          <el-image
            :src="p.image_url || p.image_path"
            fit="contain"
            lazy
            class="poster-img"
            :preview-src-list="posters.map((x) => x.image_url || x.image_path)"
            :initial-index="idx"
            preview-teleported
          />
          <div class="poster-meta">
            <span>#{{ p.id }} · 顺序 {{ p.sort_order }}</span>
            <span>已发 {{ p.send_count }} 次</span>
          </div>
          <div class="poster-actions" v-if="canEdit">
            <el-button size="small" :disabled="idx === 0" @click="movePoster(p, 'up')">上移</el-button>
            <el-button size="small" :disabled="idx === posters.length - 1" @click="movePoster(p, 'down')">
              下移
            </el-button>
            <el-button size="small" type="primary" plain @click="togglePoster(p)">
              {{ p.is_active ? "停用" : "启用" }}
            </el-button>
            <el-popconfirm title="确定删除这张海报？" @confirm="removePoster(p)">
              <template #reference>
                <el-button size="small" type="danger" plain>删除</el-button>
              </template>
            </el-popconfirm>
          </div>
          <el-tag
            class="poster-status"
            size="small"
            :type="p.is_active ? 'success' : 'info'"
            effect="light"
          >
            {{ p.is_active ? "启用中" : "已停用" }}
          </el-tag>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import type { UploadRequestOptions } from "element-plus";
import {
  Plus,
  Refresh,
  Search,
  Picture,
  EditPen,
  Delete,
  Calendar,
  UserFilled,
  UploadFilled,
  VideoPlay,
  Timer,
  CircleCheck,
  SwitchButton,
} from "@element-plus/icons-vue";
import http from "../api/http";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const canEdit = computed(() => auth.has("activity.campaign.edit"));

const loading = ref(false);
const saving = ref(false);
const uploading = ref(false);
const items = ref<any[]>([]);
const stats = ref<Record<string, number>>({});
const keyword = ref("");
const filterStatus = ref("");
const filterEffective = ref("");
const audienceChoices = ref<string[]>(["通用"]);

const formVisible = ref(false);
const formMode = ref<"create" | "edit">("create");
const editingId = ref<number | null>(null);
const form = reactive({
  name: "",
  range: [] as string[],
  audience_unit_types: ["通用"] as string[],
  rules: "",
  status: "enabled",
  priority: 0,
});

const posterVisible = ref(false);
const posterLoading = ref(false);
const posterCamp = ref<any>(null);
const posters = ref<any[]>([]);

const statCards = [
  { key: "running", label: "进行中", color: "#67C23A", icon: VideoPlay },
  { key: "upcoming", label: "未开始", color: "#409EFF", icon: Timer },
  { key: "ended", label: "已结束", color: "#909399", icon: CircleCheck },
  { key: "disabled", label: "已关闭", color: "#E6A23C", icon: SwitchButton },
];

function effTagType(s: string) {
  if (s === "running") return "success";
  if (s === "upcoming") return "primary";
  if (s === "ended") return "info";
  return "warning";
}

function fmtRange(a?: string, b?: string) {
  const short = (t?: string) => (t ? t.replace(/:\d{2}$/, "").slice(0, 16) : "—");
  return `${short(a)} → ${short(b)}`;
}

function toggleEffective(key: string) {
  filterEffective.value = filterEffective.value === key ? "" : key;
  loadList();
}

async function loadStats() {
  try {
    const { data } = await http.get("/api/op/campaigns/stats");
    if (data.code === 200) stats.value = data.data || {};
  } catch {
    /* ignore */
  }
}

async function loadAudience() {
  try {
    const { data } = await http.get("/api/op/campaigns/meta/audience-choices");
    if (data.code === 200) audienceChoices.value = data.data?.choices || ["通用"];
  } catch {
    audienceChoices.value = ["通用"];
  }
}

async function loadList() {
  loading.value = true;
  try {
    const { data } = await http.get("/api/op/campaigns", {
      params: {
        q: keyword.value || undefined,
        status: filterStatus.value || undefined,
        effective: filterEffective.value || undefined,
      },
    });
    if (data.code !== 200) throw new Error(data.message);
    items.value = data.data?.items || [];
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "加载失败");
  } finally {
    loading.value = false;
  }
}

async function reload() {
  await Promise.all([loadStats(), loadList()]);
}

function resetForm() {
  form.name = "";
  form.range = [];
  form.audience_unit_types = ["通用"];
  form.rules = "";
  form.status = "enabled";
  form.priority = 0;
  editingId.value = null;
}

function openCreate() {
  formMode.value = "create";
  resetForm();
  formVisible.value = true;
}

function openEdit(item: any) {
  formMode.value = "edit";
  editingId.value = item.id;
  form.name = item.name;
  form.range = [item.start_at, item.end_at].filter(Boolean) as string[];
  form.audience_unit_types = [...(item.audience_unit_types || ["通用"])];
  form.rules = item.rules || "";
  form.status = item.status || "enabled";
  form.priority = Number(item.priority || 0);
  formVisible.value = true;
}

async function saveForm() {
  if (!form.name.trim()) {
    ElMessage.warning("请填写活动名称");
    return;
  }
  if (!form.range?.length || form.range.length < 2) {
    ElMessage.warning("请选择活动时间");
    return;
  }
  if (!form.audience_unit_types?.length) {
    ElMessage.warning("请至少选择一种面向客户类型");
    return;
  }
  saving.value = true;
  try {
    const payload = {
      name: form.name.trim(),
      start_at: form.range[0],
      end_at: form.range[1],
      audience_unit_types: form.audience_unit_types,
      rules: form.rules,
      status: form.status,
      priority: form.priority,
    };
    const { data } =
      formMode.value === "create"
        ? await http.post("/api/op/campaigns", payload)
        : await http.put(`/api/op/campaigns/${editingId.value}`, payload);
    if (data.code !== 200) throw new Error(data.message);
    ElMessage.success(data.message || "已保存");
    formVisible.value = false;
    await reload();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "保存失败");
  } finally {
    saving.value = false;
  }
}

async function toggleStatus(item: any, on: boolean) {
  try {
    const { data } = await http.patch(`/api/op/campaigns/${item.id}/status`, {
      status: on ? "enabled" : "disabled",
    });
    if (data.code !== 200) throw new Error(data.message);
    ElMessage.success(on ? "已开启" : "已关闭");
    await reload();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "更新失败");
  }
}

function onSwitchChange(item: any, val: string | number | boolean) {
  void toggleStatus(item, !!val);
}

async function removeCamp(item: any) {
  try {
    const { data } = await http.delete(`/api/op/campaigns/${item.id}`);
    if (data.code !== 200) throw new Error(data.message);
    ElMessage.success("已删除");
    await reload();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "删除失败");
  }
}

async function openPosters(item: any) {
  posterCamp.value = item;
  posterVisible.value = true;
  await loadPosters(item.id);
}

async function loadPosters(id: number) {
  posterLoading.value = true;
  try {
    const { data } = await http.get(`/api/op/campaigns/${id}/posters`);
    if (data.code !== 200) throw new Error(data.message);
    posters.value = data.data?.items || [];
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "加载海报失败");
  } finally {
    posterLoading.value = false;
  }
}

async function doUpload(opt: UploadRequestOptions) {
  if (!posterCamp.value) return;
  uploading.value = true;
  try {
    const fd = new FormData();
    fd.append("files", opt.file as File);
    const { data } = await http.post(
      `/api/op/campaigns/${posterCamp.value.id}/posters`,
      fd,
      { headers: { "Content-Type": "multipart/form-data" } }
    );
    if (data.code !== 200) throw new Error(data.message);
    ElMessage.success(data.message || "上传成功");
    await loadPosters(posterCamp.value.id);
    await reload();
    opt.onSuccess?.(data as any);
  } catch (e: any) {
    const msg = e?.response?.data?.message || e?.message || "上传失败";
    ElMessage.error(msg);
    opt.onError?.(e as any);
  } finally {
    uploading.value = false;
  }
}

async function movePoster(p: any, move: "up" | "down") {
  if (!posterCamp.value) return;
  try {
    const { data } = await http.patch(
      `/api/op/campaigns/${posterCamp.value.id}/posters/${p.id}`,
      null,
      { params: { move } }
    );
    if (data.code !== 200) throw new Error(data.message);
    await loadPosters(posterCamp.value.id);
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "调整失败");
  }
}

async function togglePoster(p: any) {
  if (!posterCamp.value) return;
  try {
    const { data } = await http.patch(
      `/api/op/campaigns/${posterCamp.value.id}/posters/${p.id}`,
      null,
      { params: { is_active: !p.is_active } }
    );
    if (data.code !== 200) throw new Error(data.message);
    await loadPosters(posterCamp.value.id);
    await reload();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "更新失败");
  }
}

async function removePoster(p: any) {
  if (!posterCamp.value) return;
  try {
    const { data } = await http.delete(
      `/api/op/campaigns/${posterCamp.value.id}/posters/${p.id}`
    );
    if (data.code !== 200) throw new Error(data.message);
    ElMessage.success("已删除");
    await loadPosters(posterCamp.value.id);
    await reload();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "删除失败");
  }
}

onMounted(async () => {
  await loadAudience();
  await reload();
});
</script>

<style scoped>
.camp-page {
  max-width: 1400px;
}

.toolbar {
  display: flex;
  gap: 0.65rem;
  flex-wrap: wrap;
}

.stats-row {
  margin-bottom: 1rem;
}

.art-stat-card {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  min-height: 7.5rem;
  padding: 1.15rem 1.25rem;
  margin-bottom: 0.75rem;
  background: var(--op-card);
  border: 1px solid var(--op-card-border, rgba(0, 0, 0, 0.08));
  border-radius: calc(var(--op-radius, 8px) + 4px);
  box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.03), 0 1px 2px -1px rgba(0, 0, 0, 0.06);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
}

.art-stat-card:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--op-card-border, #e2e8f0));
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
  transform: translateY(-1px);
}

.art-stat-card.active {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--op-card-border, #e2e8f0));
  background: color-mix(in srgb, var(--accent) 7%, var(--op-card));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 25%, transparent);
}

.art-stat-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.art-stat-label {
  color: var(--op-muted, #64748b);
  font-size: 0.875rem;
  line-height: 1.3;
}

.art-stat-val {
  margin-top: 0.45rem;
  font-size: 1.65rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--op-ink, #0f172a);
  line-height: 1.15;
}

.art-stat-hint {
  margin-top: 0.35rem;
  font-size: 0.75rem;
  color: var(--op-muted, #94a3b8);
}

.art-stat-icon {
  flex-shrink: 0;
  width: 3.15rem;
  height: 3.15rem;
  border-radius: 0.75rem;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}

.filter-card {
  margin-bottom: 1rem;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
}

.camp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 1rem;
  min-height: 160px;
}

.camp-card {
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.camp-cover {
  position: relative;
  height: 168px;
  background: linear-gradient(145deg, var(--op-hover), var(--op-surface));
}

.cover-img {
  width: 100%;
  height: 168px;
}

.cover-placeholder {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  color: var(--op-muted);
  font-size: 0.85rem;
}

.eff-tag {
  position: absolute;
  top: 10px;
  left: 10px;
}

.camp-body {
  padding: 0.95rem 1rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  flex: 1;
}

.camp-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.camp-name {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.meta-line {
  display: flex;
  align-items: flex-start;
  gap: 0.4rem;
  color: var(--op-muted);
  font-size: 0.82rem;
  line-height: 1.4;
}

.meta-line .el-icon {
  margin-top: 2px;
  flex-shrink: 0;
}

.audience {
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.chip-row {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

.rules-preview {
  margin: 0.15rem 0 0;
  font-size: 0.8rem;
  color: var(--op-muted);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 2.3em;
}

.camp-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-top: auto;
  padding-top: 0.55rem;
  border-top: 1px dashed var(--op-border);
}

.camp-form :deep(.el-form-item) {
  margin-bottom: 1.1rem;
}

.form-hint {
  margin-bottom: 0.75rem;
}

.audience-group {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.audience-group :deep(.el-checkbox) {
  margin-right: 0;
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}

.poster-upload {
  margin: 0.85rem 0 1.1rem;
}

.poster-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 0.85rem;
  min-height: 120px;
}

.poster-item {
  position: relative;
  padding: 0.55rem;
  overflow: hidden;
}

.poster-img {
  width: 100%;
  height: 160px;
  background: var(--op-surface);
  border-radius: 8px;
}

.poster-meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: var(--op-muted);
  margin-top: 0.45rem;
}

.poster-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
  margin-top: 0.5rem;
}

.poster-status {
  position: absolute;
  top: 12px;
  right: 12px;
}
</style>
