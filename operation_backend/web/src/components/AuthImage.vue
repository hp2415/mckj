<template>
  <el-image
    v-if="displaySrc"
    :src="displaySrc"
    :fit="fit"
    :class="imgClass"
    :preview-src-list="previewList"
    :initial-index="initialIndex"
    preview-teleported
  />
  <div v-else-if="loading" class="auth-img-loading" :class="imgClass">
    <el-icon class="is-loading"><Loading /></el-icon>
  </div>
  <div v-else-if="failed" class="auth-img-failed" :class="imgClass">
    <slot name="error">加载失败</slot>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { Loading } from "@element-plus/icons-vue";
import http from "../api/http";

const props = withDefaults(
  defineProps<{
    src?: string | null;
    fit?: "fill" | "contain" | "cover" | "none" | "scale-down";
    imgClass?: string;
    previewSrcList?: string[];
    initialIndex?: number;
  }>(),
  {
    src: "",
    fit: "cover",
    imgClass: "",
    previewSrcList: undefined,
    initialIndex: 0,
  }
);

const displaySrc = ref("");
const previewList = ref<string[]>([]);
const loading = ref(false);
const failed = ref(false);
const blobUrls: string[] = [];

function revokeAll() {
  for (const u of blobUrls) URL.revokeObjectURL(u);
  blobUrls.length = 0;
}

async function loadOne(url: string): Promise<string> {
  if (!url) return "";
  if (!url.startsWith("/api/")) return url;
  const { data } = await http.get(url, { responseType: "blob" });
  const obj = URL.createObjectURL(data);
  blobUrls.push(obj);
  return obj;
}

async function reload() {
  revokeAll();
  displaySrc.value = "";
  previewList.value = [];
  failed.value = false;
  const src = (props.src || "").trim();
  if (!src) return;
  loading.value = true;
  try {
    displaySrc.value = await loadOne(src);
    const previews = props.previewSrcList?.length ? props.previewSrcList : [src];
    const out: string[] = [];
    for (const p of previews) {
      out.push(await loadOne(p));
    }
    previewList.value = out;
  } catch {
    failed.value = true;
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.src, props.previewSrcList?.join("|")] as const,
  () => {
    void reload();
  },
  { immediate: true }
);

onBeforeUnmount(revokeAll);
</script>

<style scoped>
.auth-img-loading,
.auth-img-failed {
  display: grid;
  place-items: center;
  background: #f8fafc;
  color: var(--op-muted);
  font-size: 0.8rem;
}
</style>
