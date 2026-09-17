import {
  DataAnalysis,
  User,
  Notebook,
  Ticket,
  OfficeBuilding,
  Present,
  Menu,
  Setting,
  Document,
  Folder,
  Link,
  Monitor,
  Grid,
  List,
  Key,
  Lock,
  Bell,
  ChatDotRound,
  Phone,
  Goods,
  Histogram,
  PieChart,
  TrendCharts,
  House,
} from "@element-plus/icons-vue";
import type { Component } from "vue";

const ICON_MAP: Record<string, Component> = {
  DataAnalysis,
  User,
  Notebook,
  Ticket,
  OfficeBuilding,
  Present,
  Menu,
  Setting,
  Document,
  Folder,
  Link,
  Monitor,
  Grid,
  List,
  Key,
  Lock,
  Bell,
  ChatDotRound,
  Phone,
  Goods,
  Histogram,
  PieChart,
  TrendCharts,
  House,
};

export const MENU_ICON_OPTIONS = Object.keys(ICON_MAP).sort();

export function resolveMenuIcon(name?: string | null): Component {
  if (!name) return Document;
  return ICON_MAP[name] || Document;
}
