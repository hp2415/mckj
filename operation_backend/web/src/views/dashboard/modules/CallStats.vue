<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>外呼</h4>
        <p>通话记录 · 次数与时长</p>
      </div>
    </div>
    <div class="stats">
      <div class="stat">
        <div class="stat-icon" style="background: rgba(38,126,240,0.12); color: #267EF0">
          <el-icon :size="18"><Phone /></el-icon>
        </div>
        <div>
          <div class="stat-val">{{ callCount }}</div>
          <div class="stat-label">外呼次数</div>
        </div>
      </div>
      <div class="stat">
        <div class="stat-icon" style="background: rgba(20,222,186,0.12); color: #14DEBA">
          <el-icon :size="18"><Timer /></el-icon>
        </div>
        <div>
          <div class="stat-val">{{ durationText }}</div>
          <div class="stat-label">通话时长</div>
        </div>
      </div>
    </div>
    <div class="avg" v-if="avgSec != null">
      平均单次 <strong>{{ avgSec }}</strong> 秒
      <span v-if="unassignedHint" class="hint">{{ unassignedHint }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { Phone, Timer } from "@element-plus/icons-vue";

const props = defineProps<{
  calls: {
    call_count?: number;
    call_seconds?: number;
    unassigned_call_count?: number;
  } | null;
}>();

const callCount = computed(() => {
  const n = props.calls?.call_count;
  if (n == null) return "—";
  return Number(n).toLocaleString("zh-CN");
});

const durationText = computed(() => {
  const s = Number(props.calls?.call_seconds || 0);
  if (!s) return "0 分钟";
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m} 分 ${rem} 秒` : `${m} 分钟`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h} 小时 ${mm} 分`;
});

const avgSec = computed(() => {
  const c = Number(props.calls?.call_count || 0);
  const s = Number(props.calls?.call_seconds || 0);
  if (!c) return null;
  return Math.round(s / c);
});

const unassignedHint = computed(() => {
  const u = Number(props.calls?.unassigned_call_count || 0);
  if (!u) return "";
  return `· 未归属 ${u} 通`;
});
</script>

<style scoped>
.biz-card {
  background: var(--op-card);
  border: 1px solid var(--op-card-border);
  border-radius: calc(var(--op-radius) + 4px);
  padding: 1.15rem 1.25rem;
  margin-bottom: 1rem;
  box-shadow: var(--op-shadow);
  min-height: 300px;
}

.biz-card-header h4 {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 650;
}

.biz-card-header p {
  margin: 0.25rem 0 0;
  font-size: 0.8rem;
  color: var(--op-muted);
}

.stats {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  margin-top: 1.25rem;
}

.stat {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  padding: 0.85rem 1rem;
  border: 1px solid var(--op-card-border);
  border-radius: 12px;
}

.stat-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.stat-val {
  font-size: 1.35rem;
  font-weight: 700;
}

.stat-label {
  font-size: 0.8rem;
  color: var(--op-muted);
  margin-top: 0.15rem;
}

.avg {
  margin-top: 1.25rem;
  font-size: 0.85rem;
  color: var(--op-muted);
}

.avg strong {
  color: var(--op-ink);
}

.hint {
  margin-left: 0.35rem;
}
</style>
