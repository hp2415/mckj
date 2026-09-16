<template>
  <div class="login-page">
    <div class="login-visual" aria-hidden="true">
      <div class="visual-glow" />
      <div class="visual-grid" />
      <div class="visual-content">
        <img class="op-logo lg brand-logo" src="/favicon.ico" alt="米宝" />
        <h1>米宝运营后台</h1>
        <p>看清团队使用痕迹，管好组织与账号开通。</p>
        <ul class="feature-list">
          <li>
            <el-icon><DataLine /></el-icon>
            销售使用率与人员活跃
          </li>
          <li>
            <el-icon><OfficeBuilding /></el-icon>
            多级部门与权限范围
          </li>
          <li>
            <el-icon><Ticket /></el-icon>
            邀请码与人事建号
          </li>
        </ul>
      </div>
    </div>

    <div class="login-panel">
      <div class="panel-inner">
        <div class="mobile-brand">
          <img class="op-logo" src="/favicon.ico" alt="米宝" />
          <span>米宝运营后台</span>
        </div>
        <h2>欢迎回来</h2>
        <p class="panel-sub">使用与桌面端相同的账号登录。未开通运营权限时无法进入。</p>

        <el-form class="login-form" size="large" @submit.prevent="onSubmit">
          <el-form-item>
            <el-input
              v-model="username"
              placeholder="用户名"
              autocomplete="username"
              clearable
              :prefix-icon="User"
            />
          </el-form-item>
          <el-form-item>
            <el-input
              v-model="password"
              type="password"
              placeholder="密码"
              autocomplete="current-password"
              show-password
              :prefix-icon="Lock"
              @keyup.enter="onSubmit"
            />
          </el-form-item>
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
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { User, Lock, DataLine, OfficeBuilding, Ticket } from "@element-plus/icons-vue";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();
const username = ref("");
const password = ref("");
const loading = ref(false);

async function onSubmit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning("请输入用户名和密码");
    return;
  }
  loading.value = true;
  try {
    await auth.login(username.value.trim(), password.value);
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
.login-page {
  min-height: 100vh;
  height: 100vh;
  display: grid;
  grid-template-columns: 1.05fr 1fr;
  background: #fff;
  overflow: auto;
}

.login-visual {
  position: relative;
  overflow: hidden;
  color: #e8f2fe;
  background:
    radial-gradient(1100px 560px at -10% -20%, rgba(var(--op-brand-rgb), 0.45), transparent 55%),
    radial-gradient(800px 480px at 110% 40%, rgba(26, 95, 204, 0.35), transparent 50%),
    linear-gradient(155deg, #0b1f3a 0%, #12325c 45%, #1a4a8c 100%);
  padding: 3.5rem 3rem;
  display: flex;
  align-items: center;
}

.visual-glow {
  position: absolute;
  inset: auto -18% -28% auto;
  width: 420px;
  height: 420px;
  border-radius: 50%;
  background: rgba(var(--op-brand-rgb), 0.28);
  filter: blur(40px);
}

.visual-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.045) 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.55), transparent 85%);
}

.visual-content {
  position: relative;
  z-index: 1;
  max-width: 420px;
}

.brand-logo {
  margin-bottom: 1.5rem;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
}

.visual-content h1 {
  margin: 0 0 0.75rem;
  font-size: clamp(2rem, 3vw, 2.55rem);
  line-height: 1.15;
  letter-spacing: -0.03em;
  color: #fff;
}

.visual-content > p {
  margin: 0 0 2rem;
  color: rgba(232, 242, 254, 0.88);
  font-size: 1.05rem;
  line-height: 1.6;
}

.feature-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.85rem;
}

.feature-list li {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.85rem 1rem;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.07);
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.login-panel {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem;
  background:
    radial-gradient(600px 320px at 90% 0%, rgba(var(--op-brand-rgb), 0.08), transparent 60%),
    #fff;
}

.panel-inner {
  width: min(100%, 400px);
}

.mobile-brand {
  display: none;
  align-items: center;
  gap: 0.65rem;
  font-weight: 700;
  margin-bottom: 1.5rem;
}

.panel-inner h2 {
  margin: 0 0 0.4rem;
  font-size: 1.75rem;
  letter-spacing: -0.03em;
}

.panel-sub {
  margin: 0 0 1.75rem;
  color: var(--op-muted);
  line-height: 1.55;
  font-size: 0.95rem;
}

.login-form :deep(.el-input__wrapper) {
  border-radius: 12px;
  box-shadow: 0 0 0 1px var(--op-border) inset;
  padding: 4px 12px;
}

.login-form :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--op-brand) inset !important;
}

.submit-btn {
  width: 100%;
  margin-top: 0.35rem;
  height: 46px;
  border-radius: 12px;
  font-weight: 600;
}

.panel-foot {
  margin-top: 1.5rem;
  text-align: center;
  color: var(--op-muted);
  font-size: 0.92rem;
}

.panel-foot a {
  margin-left: 0.35rem;
  font-weight: 600;
}

@media (max-width: 900px) {
  .login-page {
    grid-template-columns: 1fr;
  }
  .login-visual {
    display: none;
  }
  .mobile-brand {
    display: flex;
  }
}
</style>
