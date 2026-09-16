import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "element-plus/theme-chalk/dark/css-vars.css";
import "./styles/theme.css";
import App from "./App.vue";
import router from "./router";
import { applyColorMode, getStoredColorMode } from "./utils/theme";

applyColorMode(getStoredColorMode());

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(ElementPlus, { size: "default" });
app.mount("#app");
