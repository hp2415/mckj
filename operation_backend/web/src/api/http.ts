import axios from "axios";
import { useAuthStore } from "../stores/auth";

const http = axios.create({
  baseURL: "",
  timeout: 30000,
});

http.interceptors.request.use((config) => {
  const auth = useAuthStore();
  if (auth.token) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${auth.token}`;
  }
  return config;
});

http.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err?.response?.status === 401) {
      const headers = err?.config?.headers || {};
      const authHeader = headers.Authorization || headers.authorization;
      // 无凭证请求的 401（如退出后残留请求）不强制清会话
      if (authHeader) {
        const auth = useAuthStore();
        auth.logout();
        if (location.pathname !== "/login") location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);

export default http;
