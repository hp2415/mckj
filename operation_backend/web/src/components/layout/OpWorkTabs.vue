<template>
  <div class="work-tabs">
    <div class="tab-scroll" ref="scrollRef">
      <button
        v-for="tab in worktab.opened"
        :key="tab.path"
        type="button"
        class="work-tab"
        :class="{ active: tab.path === activePath }"
        @click="router.push(tab.path)"
        @contextmenu.prevent="onContext(tab.path)"
      >
        <span class="tab-title">{{ tab.title }}</span>
        <span
          v-if="worktab.opened.length > 1"
          class="tab-close"
          @click.stop="closeTab(tab.path)"
        >
          <el-icon :size="10"><Close /></el-icon>
        </span>
      </button>
    </div>
    <el-dropdown trigger="click" @command="onCommand">
      <button type="button" class="tabs-more" title="标签操作">
        <el-icon :size="16"><ArrowDown /></el-icon>
      </button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="refresh">刷新当前页</el-dropdown-item>
          <el-dropdown-item command="others" :disabled="worktab.opened.length <= 1">
            关闭其他
          </el-dropdown-item>
          <el-dropdown-item command="all" :disabled="worktab.opened.length <= 1">
            关闭全部
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ArrowDown, Close } from "@element-plus/icons-vue";
import { useWorktabStore } from "../../stores/worktab";

const emit = defineEmits<{ refresh: [] }>();

const route = useRoute();
const router = useRouter();
const worktab = useWorktabStore();
const activePath = computed(() => route.path);

function closeTab(path: string) {
  const next = worktab.removeTab(path);
  if (path === route.path && next) router.push(next);
}

function onCommand(cmd: string) {
  if (cmd === "refresh") {
    emit("refresh");
    return;
  }
  if (cmd === "others") {
    worktab.removeOthers(route.path);
    return;
  }
  if (cmd === "all") {
    const keep = worktab.opened[0];
    worktab.removeOthers(keep?.path || route.path);
    if (keep && keep.path !== route.path) router.push(keep.path);
  }
}

function onContext(path: string) {
  closeTab(path);
}
</script>

<style scoped>
.work-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px 10px 20px;
  background: var(--op-card);
  user-select: none;
}

.tab-scroll {
  flex: 1;
  min-width: 0;
  display: flex;
  overflow-x: auto;
  scrollbar-width: none;
}

.tab-scroll::-webkit-scrollbar {
  display: none;
}

.work-tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 32px;
  padding: 0 8px 0 12px;
  margin-right: 6px;
  border: 1px solid var(--op-border);
  background: var(--op-surface);
  color: var(--op-muted);
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  white-space: nowrap;
  flex-shrink: 0;
  transition: color 0.15s ease, background 0.15s ease, border-color 0.15s ease;
}

.work-tab:hover {
  color: var(--op-brand);
}

.work-tab.active {
  color: var(--op-brand);
  background: var(--el-color-primary-light-9);
  border-color: transparent;
  font-weight: 600;
}

.tab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  color: var(--op-muted);
}

.tab-close:hover {
  background: var(--op-hover);
  color: var(--op-ink);
}

.tabs-more {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--op-border);
  background: var(--op-surface);
  border-radius: 8px;
  color: var(--op-muted);
  cursor: pointer;
  flex-shrink: 0;
}

.tabs-more:hover {
  color: var(--op-ink);
  background: var(--op-hover);
}
</style>
