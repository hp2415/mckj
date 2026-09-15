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
        { path: "dashboard", component: () => import("../views/DashboardView.vue"), meta: { perm: "usage.dashboard.view" } },
        { path: "people", component: () => import("../views/PeopleView.vue"), meta: { perm: "usage.person.list" } },
        { path: "people/:id", component: () => import("../views/PersonDetailView.vue"), meta: { perm: "usage.person.detail" } },
        { path: "org", component: () => import("../views/OrgView.vue"), meta: { perm: "org.dept.manage" } },
        { path: "accounts", component: () => import("../views/AccountsView.vue"), meta: { perm: "org.roster.view" } },
        { path: "invites", component: () => import("../views/InvitesView.vue"), meta: { perm: "org.invite.manage" } },
        { path: "placeholder", component: () => import("../views/PlaceholderView.vue") },
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
    // 人事无大屏权限时落到账号页
    if (auth.has("org.roster.view")) return "/accounts";
    return "/placeholder";
  }
  return true;
});

export default router;
