<template>
  <div
    class="app-layout"
    :class="{ collapsed: asideCollapsed, 'sidebar-open': mobileOpen }"
  >
    <div v-if="mobileOpen" class="sidebar-mask" @click="mobileOpen = false" />

    <aside class="layout-sidebar" :style="{ width: asideWidth }">
      <div class="brand" :class="{ collapsed: asideCollapsed }" @click="goHome">
        <img class="op-logo" src="/favicon.ico" alt="米宝" />
        <p class="brand-name">米宝运营</p>
      </div>

      <el-scrollbar class="menu-scroll">
        <el-menu
          :default-active="activeMenu"
          :collapse="asideCollapsed"
          :collapse-transition="false"
          router
          class="side-menu"
          @select="onMenuSelect"
        >
          <el-menu-item-group
            v-for="group in visibleGroups"
            :key="group.id || group.label"
            :title="asideCollapsed ? '' : group.label"
          >
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              <el-icon><component :is="resolveMenuIcon(item.icon)" /></el-icon>
              <template #title>{{ item.title }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </el-menu>
      </el-scrollbar>
    </aside>

    <main class="layout-main">
      <header class="layout-header">
        <div class="header-left">
          <OpIconButton :title="menuButtonTitle" @click="toggleMenu">
            <el-icon>
              <Fold v-if="menuExpanded" />
              <Expand v-else />
            </el-icon>
          </OpIconButton>
          <OpIconButton class="refresh-btn" title="刷新当前页" @click="reloadView">
            <el-icon><Refresh /></el-icon>
          </OpIconButton>
          <nav class="crumbs" aria-label="breadcrumb">
            <span class="crumb">运营后台</span>
            <span class="crumb-sep">/</span>
            <span class="crumb current">{{ currentTitle }}</span>
          </nav>
        </div>

        <div class="header-right">
          <OpIconButton
            class="full-screen-btn"
            :title="isFullscreen ? '退出全屏' : '全屏'"
            @click="toggleFullscreen"
          >
            <el-icon><FullScreen /></el-icon>
          </OpIconButton>

          <OpIconButton
            class="theme-mode-btn"
            :title="isDark ? '切换浅色' : '切换暗色'"
            @click="onToggleColorMode"
          >
            <el-icon>
              <Sunny v-if="isDark" />
              <Moon v-else />
            </el-icon>
          </OpIconButton>

          <el-popover placement="bottom-end" :width="280" trigger="click" :show-arrow="false">
            <template #reference>
              <OpIconButton title="主题色">
                <el-icon><Brush /></el-icon>
              </OpIconButton>
            </template>
            <div class="theme-panel">
              <div class="theme-title">主题色</div>
              <div class="presets">
                <button
                  v-for="p in THEME_PRESETS"
                  :key="p.color"
                  type="button"
                  class="swatch"
                  :class="{ active: primaryColor === p.color }"
                  :style="{ background: p.color }"
                  :title="p.name"
                  @click="setTheme(p.color)"
                />
              </div>
              <div class="picker-row">
                <span>自定义</span>
                <el-color-picker v-model="primaryColor" @change="onPick" />
              </div>
              <el-button size="small" text type="primary" @click="resetTheme">
                恢复企微蓝
              </el-button>
            </div>
          </el-popover>

          <el-popover
            placement="bottom-end"
            :width="240"
            trigger="hover"
            :show-arrow="false"
            :offset="10"
            popper-class="user-menu-popover"
          >
            <template #reference>
              <el-avatar :size="34" class="avatar">{{ avatarText }}</el-avatar>
            </template>
            <div class="user-panel">
              <div class="user-panel-head">
                <el-avatar :size="40" class="avatar">{{ avatarText }}</el-avatar>
                <div class="user-panel-meta">
                  <strong>{{ auth.user?.real_name || auth.user?.username }}</strong>
                  <span>{{ roleLabel }}{{ kindLabel ? ` · ${kindLabel}` : "" }}</span>
                </div>
              </div>
              <div class="user-panel-dept">
                {{ auth.user?.dept_path?.join(" / ") || "未挂部门" }}
              </div>
              <button type="button" class="logout-btn" @click="onLogout">退出登录</button>
            </div>
          </el-popover>
        </div>
      </header>

      <OpWorkTabs @refresh="reloadView" />

      <div class="layout-content">
        <router-view v-slot="{ Component }">
          <transition name="slide-left" mode="out-in">
            <component :is="Component" :key="`${route.path}:${viewKey}`" class="page-view" />
          </transition>
        </router-view>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Fold,
  Expand,
  Brush,
  Refresh,
  FullScreen,
  Moon,
  Sunny,
} from "@element-plus/icons-vue";
import { useAuthStore } from "../stores/auth";
import { useWorktabStore } from "../stores/worktab";
import {
  THEME_PRESETS,
  applyTheme,
  getStoredPrimary,
  getStoredColorMode,
  toggleColorMode,
  COLOR_MODE_EVENT,
  DEFAULT_PRIMARY,
} from "../utils/theme";
import { resolveMenuIcon } from "../utils/menuIcons";
import http from "../api/http";
import OpIconButton from "../components/layout/OpIconButton.vue";
import OpWorkTabs from "../components/layout/OpWorkTabs.vue";

type NavLeaf = {
  path: string;
  title: string;
  icon?: string;
};

type MenuGroup = {
  id?: number;
  label: string;
  items: NavLeaf[];
};

type NavNode = {
  id: number;
  title: string;
  menu_type: string;
  path?: string;
  link?: string;
  icon?: string;
  children?: NavNode[];
};

/** 接口不可用时的兜底（与种子数据一致） */
const FALLBACK_GROUPS: MenuGroup[] = [
  {
    label: "运营",
    items: [
      { path: "/dashboard", title: "经营大屏", icon: "DataAnalysis" },
      { path: "/campaigns", title: "活动管理", icon: "Present" },
      { path: "/people", title: "人员明细", icon: "User" },
    ],
  },
  {
    label: "组织",
    items: [
      { path: "/accounts", title: "账号花名册", icon: "Notebook" },
      { path: "/invites", title: "邀请码", icon: "Ticket" },
      { path: "/org", title: "部门树", icon: "OfficeBuilding" },
    ],
  },
];

const COLLAPSE_KEY = "op_menu_collapsed";
const MOBILE_BREAKPOINT = 800;

const auth = useAuthStore();
const worktab = useWorktabStore();
const route = useRoute();
const router = useRouter();
const collapsed = ref(localStorage.getItem(COLLAPSE_KEY) === "1");
const mobileOpen = ref(false);
const isMobile = ref(false);
const isFullscreen = ref(false);
const viewKey = ref(0);
const primaryColor = ref(getStoredPrimary());
const colorMode = ref(getStoredColorMode());
const isDark = computed(() => colorMode.value === "dark");
const navGroups = ref<MenuGroup[]>([]);

function navToGroups(nodes: NavNode[]): MenuGroup[] {
  return (nodes || [])
    .filter((n) => n.menu_type === "directory")
    .map((n) => ({
      id: n.id,
      label: n.title,
      items: (n.children || [])
        .filter((c) => c.menu_type === "menu" && (c.path || c.link))
        .map((c) => ({
          path: c.path || c.link || "",
          title: c.title,
          icon: c.icon,
        })),
    }))
    .filter((g) => g.items.length);
}

function fallbackVisible(): MenuGroup[] {
  const permMap: Record<string, string> = {
    "/dashboard": "usage.dashboard.view",
    "/campaigns": "activity.campaign.view",
    "/people": "usage.person.list",
    "/accounts": "org.roster.view",
    "/invites": "org.invite.manage",
    "/org": "org.dept.manage",
    "/menus": "system.menu.manage",
  };
  return FALLBACK_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((item) => {
      const code = permMap[item.path];
      return !code || auth.has(code);
    }),
  })).filter((g) => g.items.length);
}

const visibleGroups = computed(() =>
  navGroups.value.length ? navGroups.value : fallbackVisible()
);

async function loadNav() {
  try {
    const { data } = await http.get("/api/op/menus/nav");
    if (data.code === 200 && Array.isArray(data.data)) {
      navGroups.value = navToGroups(data.data);
      return;
    }
  } catch {
    /* 用兜底 */
  }
  navGroups.value = [];
}

const asideCollapsed = computed(() => !isMobile.value && collapsed.value);
const menuExpanded = computed(() =>
  isMobile.value ? mobileOpen.value : !collapsed.value
);
const menuButtonTitle = computed(() => (menuExpanded.value ? "收起菜单" : "展开菜单"));
const asideWidth = computed(() => {
  if (isMobile.value) return "230px";
  return asideCollapsed.value ? "64px" : "230px";
});

const activeMenu = computed(() => {
  if (route.path.startsWith("/people/")) return "/people";
  return route.path;
});

const currentTitle = computed(() => {
  if (route.path.startsWith("/people/")) return "个人时间线";
  return (route.meta.title as string) || "工作台";
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
    ops_assistant: "运营助理",
    other: "其他",
  };
  return m[auth.user?.dept_kind || ""] || auth.user?.dept_kind || "";
});

const avatarText = computed(() => {
  const n = auth.user?.real_name || auth.user?.username || "?";
  return n.slice(0, 1);
});

watch(
  () => route.path,
  (path) => {
    if (route.meta.public) return;
    worktab.addTab({ path, title: currentTitle.value });
  },
  { immediate: true }
);

function goHome() {
  const first = visibleGroups.value[0]?.items[0];
  router.push(first?.path || "/dashboard");
}

function toggleMenu() {
  if (isMobile.value) {
    mobileOpen.value = !mobileOpen.value;
    return;
  }
  collapsed.value = !collapsed.value;
  localStorage.setItem(COLLAPSE_KEY, collapsed.value ? "1" : "0");
}

function onMenuSelect() {
  if (isMobile.value) mobileOpen.value = false;
}

function reloadView() {
  viewKey.value += 1;
}

function setTheme(color: string) {
  primaryColor.value = applyTheme(color);
}

function onPick(val: string | null) {
  if (val) setTheme(val);
}

function resetTheme() {
  setTheme(DEFAULT_PRIMARY);
}

function onToggleColorMode(e: MouseEvent) {
  toggleColorMode(e);
}

function onColorModeEvent(e: Event) {
  const mode = (e as CustomEvent<"light" | "dark">).detail;
  if (mode === "light" || mode === "dark") colorMode.value = mode;
}

function onLogout() {
  worktab.reset();
  auth.logout();
  router.push("/login");
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await document.documentElement.requestFullscreen();
    }
  } catch {
    /* ignore */
  }
}

function syncViewport() {
  isMobile.value = window.innerWidth < MOBILE_BREAKPOINT;
  if (!isMobile.value) mobileOpen.value = false;
}

function onFullscreenChange() {
  isFullscreen.value = !!document.fullscreenElement;
}

onMounted(() => {
  syncViewport();
  loadNav();
  window.addEventListener("resize", syncViewport);
  window.addEventListener(COLOR_MODE_EVENT, onColorModeEvent);
  window.addEventListener("op-menus-changed", loadNav);
  document.addEventListener("fullscreenchange", onFullscreenChange);
});

watch(
  () => auth.user?.permissions?.join(","),
  () => {
    loadNav();
  }
);

onUnmounted(() => {
  window.removeEventListener("resize", syncViewport);
  window.removeEventListener(COLOR_MODE_EVENT, onColorModeEvent);
  window.removeEventListener("op-menus-changed", loadNav);
  document.removeEventListener("fullscreenchange", onFullscreenChange);
});
</script>

<style scoped>
.app-layout {
  display: flex;
  width: 100%;
  height: 100vh;
  background: var(--op-surface);
  overflow: hidden;
}

.sidebar-mask {
  position: fixed;
  inset: 0;
  z-index: 290;
  background: rgba(0, 0, 0, 0.45);
}

.layout-sidebar {
  flex-shrink: 0;
  height: 100vh;
  background: var(--op-card);
  border-right: 1px solid var(--op-card-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.22s ease;
  user-select: none;
}

.brand {
  display: flex;
  align-items: center;
  height: var(--op-header-h);
  padding: 0 16px 0 18px;
  cursor: pointer;
  flex-shrink: 0;
  overflow: hidden;
  gap: 10px;
}

.brand.collapsed {
  justify-content: center;
  padding: 0;
}

.brand.collapsed .brand-name {
  display: none;
}

.brand-name {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--op-ink);
  white-space: nowrap;
}

.menu-scroll {
  flex: 1;
  min-height: 0;
}

.side-menu {
  border-right: none !important;
  background: transparent !important;
  padding: 4px 0 12px;
}

.side-menu:not(.el-menu--collapse) {
  width: 100%;
}

.layout-sidebar :deep(.el-menu-item-group__title) {
  padding: 14px 22px 6px !important;
  font-size: 12px !important;
  font-weight: 600;
  color: var(--op-nav-muted) !important;
  letter-spacing: 0.04em;
  line-height: 1;
}

.layout-sidebar :deep(.el-menu--collapse .el-menu-item-group__title) {
  display: none;
}

.layout-sidebar :deep(.el-menu--collapse .el-menu-item-group) {
  padding: 0;
}

.layout-sidebar :deep(.el-menu-item) {
  width: calc(100% - 16px);
  margin: 0 8px 4px;
  height: 42px;
  line-height: 42px;
  border-radius: 8px;
  color: var(--op-nav-text);
}

.layout-sidebar :deep(.el-menu-item .el-icon) {
  font-size: 18px;
  color: inherit;
}

.layout-sidebar :deep(.el-menu-item:hover) {
  background: var(--op-hover) !important;
}

.layout-sidebar :deep(.el-menu-item.is-active) {
  color: var(--op-brand) !important;
  background: var(--el-color-primary-light-9) !important;
  font-weight: 600;
}

.layout-sidebar :deep(.el-menu--collapse) {
  width: 64px;
}

.layout-sidebar :deep(.el-menu--collapse .el-menu-item) {
  width: 48px;
  margin: 0 8px 4px;
  padding: 0 !important;
  justify-content: center;
}

.layout-main {
  flex: 1;
  min-width: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.layout-header {
  flex-shrink: 0;
  height: var(--op-header-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 0 8px;
  background: var(--op-card);
}

.header-left,
.header-right {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.crumbs {
  display: flex;
  align-items: center;
  margin-left: 6px;
  min-width: 0;
}

.crumb {
  font-size: 13px;
  color: var(--op-nav-muted);
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.crumb.current {
  color: var(--op-nav-text);
}

.crumb-sep {
  margin: 0 6px;
  color: var(--op-nav-muted);
}

.refresh-btn:hover :deep(.el-icon) {
  animation: rotate180 0.45s ease;
}

.full-screen-btn:hover :deep(.el-icon) {
  animation: expand 0.45s ease;
}

.theme-mode-btn:hover :deep(.el-icon) {
  animation: expand 0.45s ease;
}

.avatar {
  background: linear-gradient(135deg, var(--op-brand), var(--op-brand-deep));
  color: #fff;
  font-weight: 700;
  cursor: pointer;
  margin-left: 4px;
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
  border: 2px solid var(--op-card);
  box-shadow: 0 0 0 1px var(--op-border);
  cursor: pointer;
  padding: 0;
}

.swatch.active {
  box-shadow: 0 0 0 2px var(--op-brand);
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

.user-panel {
  padding: 4px 2px 2px;
}

.user-panel-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
}

.user-panel-meta {
  min-width: 0;
  line-height: 1.3;
}

.user-panel-meta strong {
  display: block;
  font-size: 14px;
}

.user-panel-meta span {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: var(--op-muted);
}

.user-panel-dept {
  padding: 10px 0 12px;
  border-top: 1px solid var(--op-border);
  font-size: 12px;
  color: var(--op-muted);
  line-height: 1.4;
}

.logout-btn {
  width: 100%;
  height: 34px;
  border: 1px solid var(--op-border);
  background: var(--op-card);
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  color: var(--op-ink);
  transition: box-shadow 0.2s ease;
}

.logout-btn:hover {
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.08);
}

.layout-content {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 4px 20px 24px;
  background: var(--op-surface);
}

.page-view {
  min-height: 100%;
}

@keyframes rotate180 {
  from {
    transform: rotate(0);
  }
  to {
    transform: rotate(180deg);
  }
}

@keyframes expand {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.12);
  }
}

@media (max-width: 800px) {
  .layout-sidebar {
    position: fixed;
    top: 0;
    left: 0;
    z-index: 300;
    width: 230px !important;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    box-shadow: 8px 0 24px rgba(15, 23, 42, 0.12);
  }

  .sidebar-open .layout-sidebar {
    transform: translateX(0);
  }

  .crumbs {
    display: none;
  }

  .layout-content {
    padding: 4px 15px 20px;
  }
}
</style>
