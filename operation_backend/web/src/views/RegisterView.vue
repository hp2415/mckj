<template>
  <div class="auth-page">
    <div class="auth-shell">
      <div class="brand-row">
        <img class="op-logo" src="/favicon.ico" alt="米宝" />
        <div>
          <div class="brand-name">米宝运营后台</div>
          <div class="brand-sub">邀请码注册</div>
        </div>
      </div>

      <el-alert
        class="tip"
        type="info"
        :closable="false"
        show-icon
        title="无需填写销售微信号；注册后是否能登录取决于邀请码授予的角色。"
      />

      <el-form class="form" label-position="top" size="large" @submit.prevent="onSubmit">
        <el-form-item label="邀请码" required>
          <el-input
            v-model="form.invite_code"
            placeholder="人事 / 老板发放"
            clearable
            :prefix-icon="Ticket"
          />
        </el-form-item>
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" clearable :prefix-icon="User" />
        </el-form-item>
        <el-form-item label="姓名" required>
          <el-input v-model="form.real_name" clearable :prefix-icon="Avatar" />
        </el-form-item>
        <el-form-item label="密码" required>
          <el-input
            v-model="form.password"
            type="password"
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

      <div class="foot">
        已有账号？
        <router-link to="/login">返回登录</router-link>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Ticket, User, Avatar, Lock } from "@element-plus/icons-vue";
import http from "../api/http";

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
.auth-page {
  min-height: 100vh;
  height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1rem;
  overflow: auto;
  background:
    radial-gradient(700px 360px at 10% 0%, rgba(var(--op-brand-rgb), 0.12), transparent 55%),
    radial-gradient(600px 300px at 100% 100%, rgba(15, 23, 42, 0.05), transparent 50%),
    var(--op-surface);
}

.auth-shell {
  width: min(100%, 460px);
  background: #fff;
  border: 1px solid var(--op-border);
  border-radius: 20px;
  box-shadow: var(--op-shadow);
  padding: 2rem 1.75rem 1.75rem;
}

.brand-row {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  margin-bottom: 1.25rem;
}

.brand-name {
  font-weight: 700;
  font-size: 1.05rem;
}

.brand-sub {
  color: var(--op-muted);
  font-size: 0.88rem;
  margin-top: 0.1rem;
}

.tip {
  margin-bottom: 1.25rem;
  border-radius: 12px;
}

.form :deep(.el-input__wrapper) {
  border-radius: 12px;
}

.submit-btn {
  width: 100%;
  height: 46px;
  border-radius: 12px;
  font-weight: 600;
  margin-top: 0.25rem;
}

.foot {
  margin-top: 1.25rem;
  text-align: center;
  color: var(--op-muted);
  font-size: 0.92rem;
}

.foot a {
  margin-left: 0.35rem;
  font-weight: 600;
}
</style>
