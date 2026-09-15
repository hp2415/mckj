<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '72px' : '232px'" class="aside">
      <div class="brand" :class="{ collapsed }">
        <img class="op-logo" src="/favicon.ico" alt="米宝" />
        <div v-if="!collapsed" class="brand-text">
          <strong>米宝运营</strong>
          <span>Operation</span>
        </div>
      </div>

      <el-scrollbar class="menu-scroll">
        <el-menu
          :default-active="activeMenu"
          :collapse="collapsed"
          :collapse-transition="false"
          router
          class="side-menu"
        >
          <el-menu-item v-if="auth.has('usage.dashboard.view')" index="/dashboard">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>使用率大屏</template>
          </el-menu-item>
          <el-menu-item v-if="auth.has('usage.person.list')" index="/people">
            <el-icon><User /></el-icon>
            <template #title>人员明细</template>
          </el-menu-item>
          <el-menu-item v-if="auth.has('org.roster.view')" index="/accounts">
            <el-icon><Notebook /></el-icon>
            <template #title>账号花名册</template>
          </el-menu-item>
          <el-menu-item v-if="auth.has('org.invite.manage')" index="/invites">
            <el-icon><Ticket /></el-icon>
            <template #title>邀请码</template>
          </el-menu-item>
          <el-menu-item v-if="auth.has('org.dept.manage')" index="/org">
            <el-icon><OfficeBuilding /></el-icon>
            <template #title>部门树</template>
          </el-menu-item>
        </el-menu>
      </el-scrollbar>

      <button class="collapse-btn" type="button" @click="collapsed = !collapsed">
        <el-icon><Fold v-if="!collapsed" /><Expand v-else /></el-icon>
      </button>
    </el-aside>

    <el-container class="main-wrap">
      <el-header class="header" height="64px">
        <div class="header-left">
          <el-breadcrumb separator="/">
            <el-breadcrumb-item>运营后台</el-breadcrumb-item>
            <el-breadcrumb-item>{{ currentTitle }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <el-popover placement="bottom-end" :width="280" trigger="click">
            <template #reference>
              <el-button circle :icon="Brush" title="主题色" />
            </template>
            <div class="theme-panel">
              <div class="theme-title">主题色</div>
              <div class="presets">
                <button
                  v-for="p in THEME_PRESETS"
                  :key="p.color"
                  type="button"
                  class="swatch"
                  :style="{ background: p.color }"
                  :title="p.name"
                  @click="setTheme(p.color)"
                />
              </div>
              <div class="picker-row">
                <span>自定义</span>
                <el-color-picker v-model="primaryColor" @change="onPick" />
              </div>
              <el-button size="small" text type="primary" @click="resetTheme">恢复企微蓝</el-button>
            </div>
          </el-popover>

          <el-tag v-if="auth.user?.dept_kind" effect="plain" round type="primary">
            {{ kindLabel }}
          </el-tag>
          <el-dropdown trigger="click">
            <div class="user-chip">
              <el-avatar :size="34" class="avatar">{{ avatarText }}</el-avatar>
              <div class="user-meta">
                <div class="name">{{ auth.user?.real_name || auth.user?.username }}</div>
                <div class="role">{{ roleLabel }}</div>
              </div>
              <el-icon><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  {{ auth.user?.dept_path?.join(" / ") || "未挂部门" }}
                </el-dropdown-item>
                <el-dropdown-item divided @click="onLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="op-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  DataAnalysis,
  User,
  Notebook,
  Ticket,
  OfficeBuilding,
  Fold,
  Expand,
  ArrowDown,
  Brush,
} from "@element-plus/icons-vue";
import { useAuthStore } from "../stores/auth";
import {
  THEME_PRESETS,
  applyTheme,
  getStoredPrimary,
  DEFAULT_PRIMARY,
} from "../utils/theme";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const collapsed = ref(false);
const primaryColor = ref(getStoredPrimary());

const titleMap: Record<string, string> = {
  "/dashboard": "使用率大屏",
  "/people": "人员明细",
  "/accounts": "账号花名册",
  "/invites": "邀请码",
  "/org": "部门树",
  "/placeholder": "模块预留",
};

const activeMenu = computed(() => {
  if (route.path.startsWith("/people/")) return "/people";
  return route.path;
});

const currentTitle = computed(() => {
  if (route.path.startsWith("/people/")) return "个人时间线";
  return titleMap[route.path] || "工作台";
});

const roleLabel = computed(() => {
  const m: Record<string, string> = {
    boss: "老板",
    manager: "经理",
    staff: "员工",
    none: "未开通",
  };
  return m[auth.user?.op_role || ""] || auth.user?.op_role || "—";
});

const kindLabel = computed(() => {
  const m: Record<string, string> = {
    sales: "销售",
    finance: "财务",
    supply: "供应链",
    hr: "人事",
    other: "其他",
  };
  return m[auth.user?.dept_kind || ""] || auth.user?.dept_kind;
});

const avatarText = computed(() => {
  const n = auth.user?.real_name || auth.user?.username || "?";
  return n.slice(0, 1);
});

function setTheme(color: string) {
  primaryColor.value = applyTheme(color);
}

function onPick(val: string | null) {
  if (val) setTheme(val);
}

function resetTheme() {
  setTheme(DEFAULT_PRIMARY);
}

function onLogout() {
  auth.logout();
  router.push("/login");
}
</script>

<style scoped>
.layout {
  height: 100vh;
  overflow: hidden;
  background: var(--op-surface);
}

.aside {
  height: 100vh;
  background: var(--op-sidebar);
  color: #e2e8f0;
  transition: width 0.2s ease;
  display: flex;
  flex-direction: column;
  border-right: 1px solid rgba(255, 255, 255, 0.04);
  overflow: hidden;
  flex-shrink: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem;
  min-height: var(--op-header-h);
  flex-shrink: 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.brand.collapsed {
  justify-content: center;
  padding-inline: 0.5rem;
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  min-width: 0;
}

.brand-text strong {
  font-size: 0.98rem;
  color: #fff;
}

.brand-text span {
  font-size: 0.72rem;
  color: #94a3b8;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.menu-scroll {
  flex: 1;
  min-height: 0;
}

.side-menu {
  border-right: none !important;
  background: transparent !important;
  padding: 0.5rem 0.55rem 0.25rem;
}

.side-menu:not(.el-menu--collapse) {
  width: 100%;
}

.aside :deep(.el-menu-item) {
  border-radius: 10px;
  margin-bottom: 4px;
  color: #cbd5e1;
  height: 44px;
}

.aside :deep(.el-menu-item:hover) {
  background: var(--op-sidebar-hover) !important;
  color: #fff;
}

.aside :deep(.el-menu-item.is-active) {
  background: linear-gradient(
    90deg,
    rgba(var(--op-brand-rgb), 0.28),
    rgba(var(--op-brand-rgb), 0.08)
  ) !important;
  color: #fff !important;
}

.collapse-btn {
  margin: 0.75rem;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: #94a3b8;
  border-radius: 10px;
  height: 36px;
  cursor: pointer;
  flex-shrink: 0;
}

.collapse-btn:hover {
  color: #fff;
  border-color: rgba(var(--op-brand-rgb), 0.45);
}

.main-wrap {
  min-width: 0;
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255, 255, 255, 0.96);
  border-bottom: 1px solid var(--op-border);
  padding: 0 1.25rem;
  z-index: 10;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  cursor: pointer;
  padding: 0.25rem 0.4rem 0.25rem 0.25rem;
  border-radius: 999px;
}

.user-chip:hover {
  background: #f1f5f9;
}

.avatar {
  background: linear-gradient(135deg, var(--op-brand), var(--op-brand-deep));
  color: #fff;
  font-weight: 700;
}

.user-meta {
  line-height: 1.2;
}

.user-meta .name {
  font-size: 0.9rem;
  font-weight: 600;
}

.user-meta .role {
  font-size: 0.75rem;
  color: var(--op-muted);
}

.theme-panel {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.theme-title {
  font-weight: 600;
  font-size: 0.92rem;
}

.presets {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.swatch {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px var(--op-border);
  cursor: pointer;
  padding: 0;
}

.swatch:hover {
  transform: scale(1.08);
}

.picker-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--op-muted);
  font-size: 0.88rem;
}
</style>
