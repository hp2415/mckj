<template>
  <div class="wrap">
    <el-result icon="info" title="模块建设中" sub-title="财务 / 供应链等业务页将在后续版本开放。">
      <template #extra>
        <el-button type="primary" @click="$router.push('/accounts')" v-if="auth.has('org.roster.view')">
          前往账号花名册
        </el-button>
      </template>
    </el-result>
  </div>
</template>

<script setup lang="ts">
import { useAuthStore } from "../stores/auth";
const auth = useAuthStore();
</script>

<style scoped>
.wrap {
  min-height: 50vh;
  display: grid;
  place-items: center;
  background: #fff;
  border: 1px solid var(--op-border);
  border-radius: var(--op-radius);
}
</style>
