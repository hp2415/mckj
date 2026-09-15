import { defineStore } from "pinia";
import { ref, computed } from "vue";
import http from "../api/http";

export type OpUser = {
  user_id: number;
  username: string;
  real_name: string;
  op_role: string;
  dept_id: number | null;
  dept_kind: string | null;
  dept_path: string[];
  permissions: string[];
  is_desktop_admin?: boolean;
};

export const useAuthStore = defineStore("auth", () => {
  const token = ref<string>(localStorage.getItem("op_token") || "");
  const user = ref<OpUser | null>(null);

  const isLogin = computed(() => !!token.value);
  const perms = computed(() => new Set(user.value?.permissions || []));

  function has(code: string) {
    return perms.value.has(code);
  }

  async function login(username: string, password: string) {
    const { data } = await http.post("/api/op/auth/login", { username, password });
    if (data.code !== 200) throw new Error(data.message || "登录失败");
    token.value = data.data.access_token;
    user.value = data.data.user;
    localStorage.setItem("op_token", token.value);
  }

  async function fetchMe() {
    if (!token.value) return;
    const { data } = await http.get("/api/op/auth/me");
    if (data.code === 200) user.value = data.data;
  }

  function logout() {
    token.value = "";
    user.value = null;
    localStorage.removeItem("op_token");
  }

  return { token, user, isLogin, has, login, fetchMe, logout };
});
