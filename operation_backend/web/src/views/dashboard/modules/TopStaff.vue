<template>
  <div class="biz-card">
    <div class="biz-card-header">
      <div>
        <h4>成单人员 Top</h4>
        <p>{{ subtitle }}</p>
      </div>
    </div>
    <el-table
      :data="items || []"
      size="small"
      class="top-table"
      :class="{ 'is-scrollable': isScrollable }"
      :max-height="isScrollable ? TABLE_SCROLL_MAX : undefined"
      :header-cell-style="{ background: 'transparent', color: 'var(--op-muted)' }"
      @row-click="onRow"
    >
      <el-table-column type="index" width="42" />
      <el-table-column prop="name" label="姓名" min-width="90" />
      <el-table-column label="成单金额" min-width="100" align="left" header-align="left">
        <template #default="{ row }">
          <span class="gmv">{{ formatMoneyShort(row.gmv) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="order_count" label="单数" width="64" align="right" />
      <el-table-column prop="friends" label="好友" width="64" align="right" />
      <el-table-column label="占比" min-width="100">
        <template #default="{ row }">
          <el-progress
            :percentage="pct(row.gmv)"
            :stroke-width="8"
            :show-text="false"
            color="var(--op-brand)"
          />
        </template>
      </el-table-column>
    </el-table>
    <div v-if="!(items || []).length" class="empty">暂无成单数据</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import { formatMoneyShort } from "../chartUtils";

const props = defineProps<{
  items: Array<{
    user_id: number;
    name: string;
    gmv: number;
    order_count: number;
    friends: number;
  }> | null;
}>();

const router = useRouter();

const VISIBLE_ROWS = 10;
/** 表头约 40px + 10 行 small 行高约 40px */
const TABLE_SCROLL_MAX = 40 + VISIBLE_ROWS * 40;

const itemCount = computed(() => (props.items || []).length);
const isScrollable = computed(() => itemCount.value > VISIBLE_ROWS);
const subtitle = computed(() => {
  const n = itemCount.value;
  if (n > VISIBLE_ROWS) return `按成单金额 · 共 ${n} 人，可滑动查看`;
  return "按成单金额";
});
const maxGmv = computed(() =>
  Math.max(0, ...(props.items || []).map((x) => Number(x.gmv || 0)))
);

function pct(gmv: number) {
  if (!maxGmv.value) return 0;
  return Math.round((Number(gmv || 0) / maxGmv.value) * 100);
}

function onRow(row: { user_id: number }) {
  if (row?.user_id) router.push(`/people/${row.user_id}`);
}
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

.top-table {
  margin-top: 0.5rem;
  cursor: pointer;
}

.top-table.is-scrollable :deep(.el-table__body-wrapper) {
  overflow-y: auto;
}

.gmv {
  font-weight: 600;
}

.empty {
  text-align: center;
  color: var(--op-muted);
  padding: 2rem 0;
  font-size: 0.85rem;
}
</style>
