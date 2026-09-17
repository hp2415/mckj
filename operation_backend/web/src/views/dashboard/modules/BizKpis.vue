<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>经营概览</h4>
        <p>{{ subtitle }}</p>
      </div>
    </div>
    <el-row :gutter="16" class="tiles">
      <el-col :xs="24" :sm="12" :lg="6" v-for="item in tiles" :key="item.label">
        <div class="tile">
          <div class="tile-icon" :style="{ background: item.bg, color: item.color }">
            <el-icon :size="20"><component :is="item.icon" /></el-icon>
          </div>
          <div class="tile-body">
            <div class="tile-val">{{ item.value }}</div>
            <div class="tile-label">{{ item.label }}</div>
            <div class="tile-sub" v-if="item.hint">{{ item.hint }}</div>
            <div class="tile-change" v-if="item.change != null">
              {{ item.changeLabel }}
              <span :class="item.change >= 0 ? 'up' : 'down'">
                {{ item.change >= 0 ? "+" : "" }}{{ item.change }}%
              </span>
            </div>
          </div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { Coin, Calendar, Document, UserFilled } from "@element-plus/icons-vue";

const props = defineProps<{
  kpis: Record<string, any> | null;
  days: number;
}>();

function money(n: number | undefined | null) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  if (v >= 10000) return `¥${(v / 10000).toFixed(2)}万`;
  return `¥${v.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
}

function num(n: number | undefined | null) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString("zh-CN");
}

const subtitle = computed(() => {
  if (props.days === 1) return "今日 · 上海自然日";
  return `近 ${props.days} 天 · 上海自然日`;
});

const tiles = computed(() => {
  const k = props.kpis || {};
  return [
    {
      label: "成单金额",
      value: money(k.gmv),
      hint: k.aov != null ? `客单价 ${money(k.aov)}` : "",
      change: k.gmv_change,
      changeLabel: props.days === 1 ? "较昨天" : "较上期",
      icon: Coin,
      color: "#267EF0",
      bg: "rgba(38,126,240,0.12)",
    },
    {
      label: "今日成单",
      value: money(k.today_gmv),
      hint: k.today_order_count != null ? `${num(k.today_order_count)} 单` : "",
      change: k.today_gmv_change,
      changeLabel: "较昨天",
      icon: Calendar,
      color: "#14DEBA",
      bg: "rgba(20,222,186,0.12)",
    },
    {
      label: "有效订单",
      value: num(k.order_count),
      hint: "",
      change: k.order_count_change,
      changeLabel: props.days === 1 ? "较昨天" : "较上期",
      icon: Document,
      color: "#FFAF20",
      bg: "rgba(255,175,32,0.12)",
    },
    {
      label: "新增好友",
      value: num(k.friends),
      hint: "",
      change: k.friends_change,
      changeLabel: props.days === 1 ? "较昨天" : "较上期",
      icon: UserFilled,
      color: "#FA8A6C",
      bg: "rgba(250,138,108,0.12)",
    },
  ];
});
</script>

<style scoped>
.biz-card {
  background: var(--op-card);
  border: 1px solid var(--op-card-border);
  border-radius: calc(var(--op-radius) + 4px);
  padding: 1.15rem 1.25rem 0.85rem;
  margin-bottom: 1rem;
  box-shadow: var(--op-shadow);
  min-height: 280px;
  overflow: hidden;
}

.biz-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 0.85rem;
}

.biz-card-header h4 {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 650;
  color: var(--op-ink);
}

.biz-card-header p {
  margin: 0.25rem 0 0;
  font-size: 0.8rem;
  color: var(--op-muted);
}

.tile {
  border: 1px solid var(--op-card-border);
  border-radius: 12px;
  padding: 1.1rem 1rem;
  margin-bottom: 0.85rem;
  height: calc(100% - 0.85rem);
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
}

.tile-icon {
  width: 42px;
  height: 42px;
  border-radius: 10px;
  display: grid;
  place-items: center;
}

.tile-val {
  font-size: clamp(1.05rem, 1.8vw, 1.35rem);
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--op-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
  line-height: 1.25;
}

.tile-body {
  min-width: 0;
  overflow: hidden;
}

.tile-label {
  margin-top: 0.2rem;
  font-size: 0.9rem;
  color: var(--op-muted);
}

.tile-sub {
  font-size: 0.75rem;
  color: var(--op-muted);
  margin-top: 0.15rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tile-change {
  font-size: 0.75rem;
  color: var(--op-muted);
  margin-top: 0.35rem;
}

.tile-change .up {
  color: #13deb9;
  font-weight: 600;
}

.tile-change .down {
  color: #ff4d4f;
  font-weight: 600;
}
</style>
