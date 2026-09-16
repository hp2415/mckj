import { defineStore } from "pinia";
import { ref } from "vue";

export type WorkTab = {
  path: string;
  title: string;
};

const STORAGE_KEY = "op_worktabs";

function loadTabs(): WorkTab[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (t) => t && typeof t.path === "string" && typeof t.title === "string"
    );
  } catch {
    return [];
  }
}

export const useWorktabStore = defineStore("worktab", () => {
  const opened = ref<WorkTab[]>(loadTabs());

  function persist() {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(opened.value));
    } catch {
      /* ignore */
    }
  }

  function addTab(tab: WorkTab) {
    if (!tab.path || tab.path === "/") return;
    const i = opened.value.findIndex((t) => t.path === tab.path);
    if (i === -1) opened.value.push({ ...tab });
    else opened.value[i].title = tab.title;
    persist();
  }

  function removeTab(path: string): string | null {
    const i = opened.value.findIndex((t) => t.path === path);
    if (i === -1 || opened.value.length <= 1) return null;
    opened.value.splice(i, 1);
    persist();
    return opened.value[Math.min(i, opened.value.length - 1)]?.path ?? null;
  }

  function removeOthers(path: string) {
    const current = opened.value.find((t) => t.path === path);
    opened.value = current ? [current] : opened.value.slice(0, 1);
    persist();
  }

  function reset() {
    opened.value = [];
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }

  return { opened, addTab, removeTab, removeOthers, reset };
});
