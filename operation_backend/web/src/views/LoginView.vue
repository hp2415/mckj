<template>
  <div class="op-auth-page">
    <AuthShowcase />

    <section class="op-auth-card">
      <h2>登录</h2>
      <p class="panel-sub">使用桌面端同一账号，开通后即可进入。</p>

      <el-form class="login-form" size="large" @submit.prevent="onSubmit">
        <label class="field-label" for="login-username">用户名</label>
        <el-form-item>
          <el-input
            id="login-username"
            v-model="username"
            placeholder="请输入用户名"
            autocomplete="username"
            clearable
            :prefix-icon="User"
          />
        </el-form-item>

        <label class="field-label" for="login-password">密码</label>
        <el-form-item>
          <el-input
            id="login-password"
            v-model="password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            show-password
            :prefix-icon="Lock"
            @keyup.enter="onSubmit"
          />
        </el-form-item>

        <div class="form-meta">
          <el-checkbox v-model="remember">记住用户名</el-checkbox>
        </div>

        <el-button
          class="submit-btn"
          type="primary"
          size="large"
          :loading="loading"
          native-type="submit"
          @click="onSubmit"
        >
          登录
        </el-button>
      </el-form>

      <div class="panel-foot">
        <span>还没有账号？</span>
        <router-link to="/register">邀请码注册</router-link>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { User, Lock } from "@element-plus/icons-vue";
import { useAuthStore } from "../stores/auth";
import AuthShowcase from "../components/AuthShowcase.vue";

const REMEMBER_KEY = "op_remember_username";
const auth = useAuthStore();
const router = useRouter();
const username = ref("");
const password = ref("");
const remember = ref(false);
const loading = ref(false);

onMounted(() => {
  try {
    const saved = localStorage.getItem(REMEMBER_KEY) || "";
    if (saved) {
      username.value = saved;
      remember.value = true;
    }
  } catch {
    /* ignore */
  }
});

async function onSubmit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning("请输入用户名和密码");
    return;
  }
  loading.value = true;
  try {
    await auth.login(username.value.trim(), password.value);
    try {
      if (remember.value) localStorage.setItem(REMEMBER_KEY, username.value.trim());
      else localStorage.removeItem(REMEMBER_KEY);
    } catch {
      /* ignore */
    }
    ElMessage.success("登录成功");
    if (auth.has("usage.dashboard.view")) router.push("/dashboard");
    else if (auth.has("activity.campaign.view")) router.push("/campaigns");
    else if (auth.has("org.roster.view")) router.push("/accounts");
    else router.push("/placeholder");
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || e?.message || "登录失败");
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.op-auth-card h2 {
  margin: 0 0 0.4rem;
  font-size: 1.75rem;
  letter-spacing: -0.045em;
  line-height: 1.15;
}

.panel-sub {
  margin: 0 0 1.55rem;
  color: var(--op-muted);
  line-height: 1.55;
  font-size: 0.9rem;
}

.field-label {
  display: block;
  margin: 0 0 0.4rem;
  font-size: 0.82rem;
  font-weight: 650;
  color: var(--op-ink);
}

.login-form :deep(.el-form-item) {
  margin-bottom: 1.05rem;
}

.login-form :deep(.el-input__wrapper) {
  border-radius: 12px;
  min-height: 46px;
  box-shadow: 0 0 0 1px var(--op-border) inset;
  padding: 4px 12px;
  background: #fff;
  transition: box-shadow 0.18s ease;
}

.login-form :deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #c9d4e5 inset;
}

.login-form :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 2px rgba(var(--op-brand-rgb), 0.28), 0 0 0 1px var(--op-brand) inset !important;
}

.form-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: -0.15rem 0 1.05rem;
}

.submit-btn {
  width: 100%;
  height: 48px;
  border-radius: 12px;
  font-weight: 650;
  letter-spacing: 0.04em;
  box-shadow: 0 10px 22px rgba(var(--op-brand-rgb), 0.26);
}

.submit-btn:hover,
.submit-btn:focus {
  transform: translateY(-1px);
}

.panel-foot {
  margin-top: 1.35rem;
  text-align: center;
  color: var(--op-muted);
  font-size: 0.92rem;
}

.panel-foot a {
  margin-left: 0.35rem;
  font-weight: 650;
}

@media (prefers-reduced-motion: reduce) {
  .submit-btn:hover,
  .submit-btn:focus {
    transform: none;
  }
}
</style>
