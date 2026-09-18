<template>
  <div class="op-auth-page">
    <AuthShowcase />

    <section class="op-auth-card">
      <h2>登录</h2>
      <p class="panel-sub">使用桌面端同一账号，开通后即可进入。</p>

      <el-form class="login-form" size="large" @submit.prevent="onSubmit">
        <label class="field-label" for="login-username">用户名</label>
        <el-form-item>
          <el-autocomplete
            id="login-username"
            v-model="username"
            :fetch-suggestions="querySavedAccounts"
            placeholder="请输入用户名"
            autocomplete="username"
            clearable
            value-key="value"
            :prefix-icon="User"
            style="width: 100%"
            @select="onSelectAccount"
            @clear="onUsernameClear"
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
          <el-checkbox v-model="remember">记住密码</el-checkbox>
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
import { onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { User, Lock } from "@element-plus/icons-vue";
import { useAuthStore } from "../stores/auth";
import AuthShowcase from "../components/AuthShowcase.vue";

/** Multi-account remembered credentials: { [username]: base64(password) } */
const ACCOUNTS_KEY = "op_remember_accounts";
const LAST_USER_KEY = "op_remember_last";
/** Legacy single-username key — migrated on load then removed */
const LEGACY_USERNAME_KEY = "op_remember_username";

type SavedAccounts = Record<string, string>;

const auth = useAuthStore();
const router = useRouter();
const username = ref("");
const password = ref("");
const remember = ref(false);
const loading = ref(false);
const savedAccounts = ref<SavedAccounts>({});
/** True when password was filled from a remembered account */
const autoFilled = ref(false);

function encodePassword(plain: string): string {
  try {
    return btoa(unescape(encodeURIComponent(plain)));
  } catch {
    return "";
  }
}

function decodePassword(encoded: string): string {
  try {
    return decodeURIComponent(escape(atob(encoded)));
  } catch {
    return "";
  }
}

function loadAccounts(): SavedAccounts {
  try {
    const raw = localStorage.getItem(ACCOUNTS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as SavedAccounts;
      if (parsed && typeof parsed === "object") return parsed;
    }
  } catch {
    /* ignore */
  }
  return {};
}

function persistAccounts(accounts: SavedAccounts) {
  try {
    if (Object.keys(accounts).length === 0) {
      localStorage.removeItem(ACCOUNTS_KEY);
      localStorage.removeItem(LAST_USER_KEY);
    } else {
      localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts));
    }
  } catch {
    /* ignore */
  }
}

function applyAccount(name: string) {
  const encoded = savedAccounts.value[name];
  if (encoded == null) {
    password.value = "";
    remember.value = false;
    autoFilled.value = false;
    return;
  }
  password.value = decodePassword(encoded);
  remember.value = true;
  autoFilled.value = true;
}

function querySavedAccounts(query: string, cb: (items: { value: string }[]) => void) {
  const q = query.trim().toLowerCase();
  const names = Object.keys(savedAccounts.value);
  const matched = q ? names.filter((n) => n.toLowerCase().includes(q)) : names;
  cb(matched.map((value) => ({ value })));
}

function onSelectAccount(item: { value: string }) {
  applyAccount(item.value);
}

function onUsernameClear() {
  password.value = "";
  remember.value = false;
  autoFilled.value = false;
}

watch(username, (name) => {
  const trimmed = name.trim();
  if (!trimmed) {
    password.value = "";
    remember.value = false;
    autoFilled.value = false;
    return;
  }
  if (trimmed in savedAccounts.value) {
    applyAccount(trimmed);
  } else if (autoFilled.value) {
    // Left a remembered account — don't keep that password on a new name
    password.value = "";
    remember.value = false;
    autoFilled.value = false;
  }
});

onMounted(() => {
  savedAccounts.value = loadAccounts();

  // Migrate legacy "remember username only" entry
  try {
    const legacy = localStorage.getItem(LEGACY_USERNAME_KEY);
    if (legacy) {
      if (!(legacy in savedAccounts.value)) {
        // Username-only legacy: restore name, leave password empty
        username.value = legacy;
        remember.value = false;
      }
      localStorage.removeItem(LEGACY_USERNAME_KEY);
    }
  } catch {
    /* ignore */
  }

  try {
    const last = localStorage.getItem(LAST_USER_KEY) || "";
    if (last && last in savedAccounts.value) {
      username.value = last;
      applyAccount(last);
    } else if (!username.value) {
      const names = Object.keys(savedAccounts.value);
      if (names.length === 1) {
        username.value = names[0];
        applyAccount(names[0]);
      }
    }
  } catch {
    /* ignore */
  }
});

async function onSubmit() {
  const name = username.value.trim();
  if (!name || !password.value) {
    ElMessage.warning("请输入用户名和密码");
    return;
  }
  loading.value = true;
  try {
    await auth.login(name, password.value);
    try {
      const next = { ...savedAccounts.value };
      if (remember.value) {
        next[name] = encodePassword(password.value);
        localStorage.setItem(LAST_USER_KEY, name);
      } else {
        delete next[name];
        const last = localStorage.getItem(LAST_USER_KEY);
        if (last === name) localStorage.removeItem(LAST_USER_KEY);
      }
      savedAccounts.value = next;
      persistAccounts(next);
    } catch {
      /* ignore */
    }
    ElMessage.success("登录成功");
    if (auth.has("usage.dashboard.view")) router.push("/dashboard");
    else if (auth.has("activity.campaign.view")) router.push("/campaigns");
    else if (auth.has("org.roster.view")) router.push("/accounts");
    else if (auth.has("system.menu.manage")) router.push("/menus");
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
