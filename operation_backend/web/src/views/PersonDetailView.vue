<template>
  <div>
    <el-page-header class="back" @back="$router.back()">
      <template #content>
        <span class="page-title" style="font-size: 1.15rem">个人时间线</span>
      </template>
    </el-page-header>

    <el-card v-if="person" class="mt soft-card" shadow="never">
      <div class="profile">
        <el-avatar :size="52" class="avatar">{{ (person.name || "?").slice(0, 1) }}</el-avatar>
        <div>
          <div class="name">{{ person.name }}</div>
          <div class="sub">{{ person.username }}</div>
        </div>
      </div>
      <el-row :gutter="12" class="stats">
        <el-col :xs="12" :sm="8" :md="4" v-for="s in statItems" :key="s.label">
          <div class="stat">
            <div class="l">{{ s.label }}</div>
            <div class="v">{{ s.value }}</div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-card class="mt soft-card" shadow="never">
      <template #header>
        <div class="card-head">
          <span class="card-title">最近动态</span>
          <div class="filters">
            <el-date-picker
              v-model="dateRange"
              type="daterange"
              size="default"
              value-format="YYYY-MM-DD"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              :disabled-date="disableFuture"
              :clearable="false"
              @change="onDateChange"
            />
            <el-button :icon="Refresh" :loading="loading" @click="loadTimeline">刷新</el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!loading && !timeline.length" description="该日期范围内暂无动态" />
      <el-timeline v-else-if="timeline.length">
        <el-timeline-item
          v-for="(t, i) in timeline"
          :key="`${t.at}-${t.type}-${i}`"
          :timestamp="t.at || ''"
          placement="top"
          :type="timelineType(t.type)"
        >
          <el-tag size="small" effect="plain" class="type-tag">{{ t.type }}</el-tag>
          {{ t.summary }}
        </el-timeline-item>
      </el-timeline>

      <div v-if="total > 0" class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @size-change="onPageSizeChange"
          @current-change="loadTimeline"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { ElMessage } from "element-plus";

function todayStr() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

const route = useRoute();
const person = ref<any>(null);
const timeline = ref<any[]>([]);
const loading = ref(false);
const dateRange = ref<[string, string]>([todayStr(), todayStr()]);
const page = ref(1);
const pageSize = ref(50);
const total = ref(0);

const statItems = computed(() => {
  const p = person.value || {};
  const editPct =
    p.outbound_edit_rate != null
      ? `${(Number(p.outbound_edit_rate) * 100).toFixed(0)}%`
      : "—";
  return [
    { label: "登录", value: p.login_count ?? "—" },
    { label: "对话", value: p.chat_msgs ?? "—" },
    { label: "外发成功", value: p.outbound_sent ?? "—" },
    { label: "外发失败", value: p.outbound_failed ?? "—" },
    { label: "编辑率", value: editPct },
    { label: "任务", value: p.task_completed ?? "—" },
    { label: "群发", value: p.blast_sent ?? "—" },
    { label: "搜商品", value: p.product_search ?? "—" },
    { label: "复制", value: p.product_copy ?? "—" },
    { label: "打开", value: p.product_open ?? "—" },
    { label: "外呼", value: p.phone_dial ?? "—" },
    { label: "综合分", value: p.score ?? "—" },
  ];
});

function timelineType(t: string) {
  if (t === "chat") return "primary";
  if (t === "outbound") return "success";
  if (String(t).includes("login")) return "warning";
  return "info";
}

function disableFuture(d: Date) {
  const end = new Date();
  end.setHours(23, 59, 59, 999);
  return d.getTime() > end.getTime();
}

function onDateChange() {
  page.value = 1;
  loadTimeline();
}

function onPageSizeChange() {
  page.value = 1;
  loadTimeline();
}

async function loadPerson() {
  const id = route.params.id;
  const { data } = await http.get(`/api/op/people/${id}`, {
    params: {
      days: 7,
      date_from: dateRange.value[0],
      date_to: dateRange.value[1],
      page: page.value,
      page_size: pageSize.value,
    },
  });
  person.value = data.data?.person;
  applyTimeline(data.data?.timeline);
}

async function loadTimeline() {
  loading.value = true;
  try {
    const id = route.params.id;
    const { data } = await http.get(`/api/op/people/${id}`, {
      params: {
        days: 7,
        date_from: dateRange.value?.[0] || todayStr(),
        date_to: dateRange.value?.[1] || todayStr(),
        page: page.value,
        page_size: pageSize.value,
      },
    });
    if (!person.value) person.value = data.data?.person;
    applyTimeline(data.data?.timeline);
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "加载失败");
  } finally {
    loading.value = false;
  }
}

function applyTimeline(payload: any) {
  // 兼容旧结构（纯数组）与新结构（分页对象）
  if (Array.isArray(payload)) {
    timeline.value = payload;
    total.value = payload.length;
    return;
  }
  timeline.value = payload?.items || [];
  total.value = Number(payload?.total || 0);
  if (payload?.page) page.value = Number(payload.page);
  if (payload?.page_size) pageSize.value = Number(payload.page_size);
}

onMounted(async () => {
  loading.value = true;
  try {
    await loadPerson();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "加载失败");
  } finally {
    loading.value = false;
  }
});
</script>

<style scoped>
.back {
  margin-bottom: 0.5rem;
}
.mt {
  margin-top: 1rem;
}
.profile {
  display: flex;
  align-items: center;
  gap: 0.9rem;
  margin-bottom: 1rem;
}
.avatar {
  background: linear-gradient(135deg, var(--op-brand), var(--op-brand-deep));
  color: #fff;
  font-weight: 700;
}
.name {
  font-size: 1.15rem;
  font-weight: 700;
}
.sub {
  color: var(--op-muted);
  font-size: 0.88rem;
}
.stats .stat {
  background: #f8fafc;
  border-radius: 12px;
  padding: 0.75rem;
  margin-bottom: 0.5rem;
}
.stats .l {
  color: var(--op-muted);
  font-size: 0.78rem;
}
.stats .v {
  font-size: 1.15rem;
  font-weight: 700;
  margin-top: 0.15rem;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.card-title {
  font-weight: 600;
}
.filters {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  flex-wrap: wrap;
}
.type-tag {
  margin-right: 0.4rem;
}
.pager {
  margin-top: 1rem;
  display: flex;
  justify-content: flex-end;
}
</style>
