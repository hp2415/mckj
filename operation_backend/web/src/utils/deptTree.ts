export type DeptItem = {
  id: number;
  parent_id: number | null;
  name: string;
  kind?: string | null;
  leader_user_id?: number | null;
  leader_name?: string | null;
  sort_order?: number;
  is_active?: boolean;
  [key: string]: any;
};

/** 扁平部门列表 → 树（按 parent_id） */
export function buildDeptTree<T extends DeptItem>(items: T[]): (T & { children: any[] })[] {
  const map = new Map<number, T & { children: any[] }>();
  for (const d of items) {
    map.set(d.id, { ...d, children: [] });
  }
  const roots: (T & { children: any[] })[] = [];
  for (const node of map.values()) {
    const pid = node.parent_id;
    if (pid != null && map.has(pid)) {
      map.get(pid)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

/** 扁平选项，缩进展示层级名称 */
export function flatDeptOptions(
  items: DeptItem[],
  indent = "　"
): { id: number; name: string; label: string }[] {
  const tree = buildDeptTree(items);
  const out: { id: number; name: string; label: string }[] = [];
  const walk = (nodes: (DeptItem & { children: any[] })[], depth: number) => {
    for (const n of nodes) {
      out.push({
        id: n.id,
        name: n.name,
        label: `${indent.repeat(depth)}${n.name}`,
      });
      if (n.children?.length) walk(n.children, depth + 1);
    }
  };
  walk(tree, 0);
  return out;
}

/** 某部门及其全部下级部门 id（含自身） */
export function collectDescendantIds(items: DeptItem[], rootId: number): Set<number> {
  const children = new Map<number, number[]>();
  for (const d of items) {
    if (d.parent_id == null) continue;
    const list = children.get(d.parent_id);
    if (list) list.push(d.id);
    else children.set(d.parent_id, [d.id]);
  }
  const out = new Set<number>([rootId]);
  const stack = [rootId];
  while (stack.length) {
    const id = stack.pop()!;
    for (const c of children.get(id) || []) {
      if (out.has(c)) continue;
      out.add(c);
      stack.push(c);
    }
  }
  return out;
}
