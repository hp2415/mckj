<template>
  <button type="button" class="op-icon-btn" :title="title">
    <slot />
  </button>
</template>

<script setup lang="ts">
defineProps<{ title?: string }>();
</script>

<style scoped>
.op-icon-btn {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 8px;
  color: var(--op-nav-icon);
  cursor: pointer;
  font-size: 18px;
  flex-shrink: 0;
  transition: background 0.2s ease, color 0.2s ease;
}

.op-icon-btn:hover {
  background: var(--op-hover);
  color: var(--op-ink);
}

.op-icon-btn:focus {
  outline: none;
}

.op-icon-btn:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--op-brand) 55%, var(--op-card));
  outline-offset: 1px;
}

.op-icon-btn :deep(.el-icon) {
  font-size: 18px;
}
</style>
