<template>
  <div class="op-auth-page">
    <AuthShowcase />

    <section class="op-auth-card">
      <h2>创建账号</h2>
      <p class="panel-sub">使用邀请码开通，角色随码生效。</p>

      <el-form class="login-form" size="large" @submit.prevent="onSubmit">
        <label class="field-label" for="reg-invite">邀请码</label>
        <el-form-item>
          <el-input
            id="reg-invite"
            v-model="form.invite_code"
            placeholder="人事 / 老板发放"
            clearable
            :prefix-icon="Ticket"
          />
        </el-form-item>

        <label class="field-label" for="reg-username">用户名</label>
        <el-form-item>
          <el-input id="reg-username" v-model="form.username" placeholder="登录用户名" clearable :prefix-icon="User" />
        </el-form-item>

        <label class="field-label" for="reg-name">姓名</label>
        <el-form-item>
          <el-input id="reg-name" v-model="form.real_name" placeholder="真实姓名" clearable :prefix-icon="Avatar" />
        </el-form-item>

        <label class="field-label" for="reg-password">密码</label>
        <el-form-item>
          <el-input
            id="reg-password"
            v-model="form.password"
            type="password"
            placeholder="设置登录密码"
            show-password
            :prefix-icon="Lock"
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
          创建账号
        </el-button>
      </el-form>

      <div class="panel-foot">
        已有账号？
        <router-link to="/login">返回登录</router-link>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Ticket, User, Avatar, Lock } from "@element-plus/icons-vue";
import http from "../api/http";
import AuthShowcase from "../components/AuthShowcase.vue";

const router = useRouter();
const loading = ref(false);
const form = reactive({
  invite_code: "",
  username: "",
  real_name: "",
  password: "",
});

async function onSubmit() {
  loading.value = true;
  try {
    const { data } = await http.post("/api/op/auth/register", {
      ...form,
      invite_code: form.invite_code.trim().toUpperCase(),
    });
    ElMessage.success(data.message || "注册成功");
    router.push("/login");
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.message || "注册失败");
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
  margin: 0 0 1.25rem;
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
  margin-bottom: 0.78rem;
}

.login-form :deep(.el-input__wrapper) {
  border-radius: 12px;
  min-height: 46px;
  box-shadow: 0 0 0 1px var(--op-border) inset;
  padding: 4px 12px;
  background: #fff;
}

.login-form :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 2px rgba(var(--op-brand-rgb), 0.28), 0 0 0 1px var(--op-brand) inset !important;
}

.submit-btn {
  width: 100%;
  height: 48px;
  border-radius: 12px;
  font-weight: 650;
  letter-spacing: 0.04em;
  box-shadow: 0 10px 22px rgba(var(--op-brand-rgb), 0.26);
  margin-top: 0.15rem;
}

.panel-foot {
  margin-top: 1.25rem;
  text-align: center;
  color: var(--op-muted);
  font-size: 0.92rem;
}

.panel-foot a {
  margin-left: 0.35rem;
  font-weight: 650;
}
</style>
