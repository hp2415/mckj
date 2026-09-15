<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">人员明细</h1>
        <p class="page-sub">范围内全员左连接各功能计数</p>
      </div>
      <div class="toolbar">
        <el-radio-group v-model="days" @change="load">
          <el-radio-button :value="7">7 天</el-radio-button>
          <el-radio-button :value="30">30 天</el-radio-button>
        </el-radio-group>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card class="soft-card" shadow="never">
      <el-table
        :data="items"
        stripe
        class="clickable"
        :header-cell-style="{ background: '#f8fafc', color: '#475569' }"
        @row-click="(r: any) => router.push(`/people/${r.user_id}`)"
      >
        <el-table-column prop="name" label="姓名" min-width="120" />
        <el-table-column prop="username" label="账号" min-width="120" />
        <el-table-column label="在线" width="72" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_online ? 'success' : 'info'" effect="plain">
              {{ row.is_online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="login_count" label="登录" width="72" align="right" />
        <el-table-column prop="chat_msgs" label="对话" width="72" align="right" />
        <el-table-column prop="outbound_sent" label="外发成功" width="84" align="right" />
        <el-table-column prop="outbound_failed" label="外发失败" width="84" align="right" />
        <el-table-column label="编辑率" width="72" align="right">
          <template #default="{ row }">
            {{ row.outbound_edit_rate != null ? `${(Number(row.outbound_edit_rate) * 100).toFixed(0)}%` : "—" }}
          </template>
        </el-table-column>
        <el-table-column prop="task_completed" label="任务" width="72" align="right" />
        <el-table-column prop="blast_sent" label="群发" width="72" align="right" />
        <el-table-column prop="product_search" label="搜商品" width="72" align="right" />
        <el-table-column prop="product_copy" label="复制" width="64" align="right" />
        <el-table-column prop="product_open" label="打开" width="64" align="right" />
        <el-table-column prop="phone_dial" label="外呼" width="64" align="right" />
        <el-table-column label="综合分" width="100" align="right">
          <template #default="{ row }">
            <el-tag round effect="light" type="success">{{ row.score }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { Refresh } from "@element-plus/icons-vue";
import http from "../api/http";
import { ElMessage } from "element-plus";

const days = ref(7);
const items = ref<any[]>([]);
const loading = ref(false);
const router = useRouter();
let onlineTimer: number | undefined;

async function load() {
  loading.value = true;
  try {
    const { data } = await http.get("/api/op/people", { params: { days: days.value } });
    items.value = data.data?.items || [];
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "加载失败");
  } finally {
    loading.value = false;
  }
}
onMounted(() => {
  load();
  onlineTimer = window.setInterval(() => {
    if (!loading.value) load();
  }, 30_000);
});
onUnmounted(() => {
  if (onlineTimer) window.clearInterval(onlineTimer);
});
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}
.clickable {
  cursor: pointer;
}
</style>
