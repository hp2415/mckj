import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "../stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", component: () => import("../views/LoginView.vue"), meta: { public: true } },
    { path: "/register", component: () => import("../views/RegisterView.vue"), meta: { public: true } },
    {
      path: "/",
      component: () => import("../views/LayoutView.vue"),
      children: [
        { path: "", redirect: "/dashboard" },
        { path: "dashboard", component: () => import("../views/dashboard/DashboardView.vue"), meta: { perm: "usage.dashboard.view", title: "经营大屏" } },
        { path: "campaigns", component: () => import("../views/CampaignsView.vue"), meta: { perm: "activity.campaign.view", title: "活动管理" } },
        { path: "people", component: () => import("../views/PeopleView.vue"), meta: { perm: "usage.person.list", title: "人员明细" } },
        { path: "people/:id", component: () => import("../views/PersonDetailView.vue"), meta: { perm: "usage.person.detail", title: "个人时间线" } },
        { path: "org", component: () => import("../views/OrgView.vue"), meta: { perm: "org.dept.manage", title: "部门树" } },
        { path: "accounts", component: () => import("../views/AccountsView.vue"), meta: { perm: "org.roster.view", title: "账号花名册" } },
        { path: "invites", component: () => import("../views/InvitesView.vue"), meta: { perm: "org.invite.manage", title: "邀请码" } },
        { path: "menus", component: () => import("../views/MenusView.vue"), meta: { perm: "system.menu.manage", title: "菜单管理" } },
        { path: "placeholder", component: () => import("../views/PlaceholderView.vue"), meta: { title: "模块预留" } },
      ],
    },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (to.meta.public) return true;
  if (!auth.token) return "/login";
  if (!auth.user) {
    try {
      await auth.fetchMe();
    } catch {
      auth.logout();
      return "/login";
    }
  }
  const perm = to.meta.perm as string | undefined;
  if (perm && !auth.has(perm)) {
    // 无当前页权限时落到有权限的首页
    if (auth.has("usage.dashboard.view")) return "/dashboard";
    if (auth.has("activity.campaign.view")) return "/campaigns";
    if (auth.has("org.roster.view")) return "/accounts";
    if (auth.has("system.menu.manage")) return "/menus";
    return "/placeholder";
  }
  return true;
});

export default router;
