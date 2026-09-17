# 运营后台（operation_backend）

独立于核心 `backend` / 桌面端的运营平台（FastAPI + Vue3 + Element Plus），共用同一 MySQL。

方案见仓库 `docs/运营后台方案.md`。

## 前置

1. 核心 backend 已能连库，且已执行迁移到 **0017**（本仓库 `backend/alembic`）。
2. 复制 `.env.example` → `.env`，`DATABASE_URL` / `SECRET_KEY` 建议与 `backend/.env` 一致。
3. 前端需要 **Node.js 18+**（当前环境若未安装，先装 Node 再 `npm install`）。
4. 当运营后台需要改动数据库时必须将改动alembic写入到backend中。

## 数据库迁移（在 backend 目录执行）

```powershell
cd d:\D\work_place\backend
..\ .venv\Scripts\python.exe -m alembic upgrade head
```

会创建 / 补齐 `op_*` 表（含 `op_dept_role_perms`、`op_menus`）、`user_activity_events`，并种子部门树与默认菜单。

## 启动 API（8010）

```powershell
cd d:\D\work_place\operation_backend
# 建议复用仓库 .venv
..\ .venv\Scripts\pip.exe install -r requirements.txt
..\ .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

健康检查：`http://127.0.0.1:8010/api/op/health`

## 启动前端（5173）

```powershell
cd d:\D\work_place\operation_backend\web
npm install
npm run dev
```

浏览器打开 Vite 地址；`/api` 已代理到 8010。

生产构建后由 FastAPI 挂载 `web/dist`：

```powershell
npm run build
# 再只启 uvicorn :8010
```



## 首次使用

1. 用现有 **SQLAdmin 超管**（`users.role=admin`）登录运营后台 → 自动等同老板。
2. 在「邀请码」为销售一组等部门生成码，或「账号花名册」直接建号。
3. 把经理挂到部门并设 `op_role=manager`。

桌面 JWT 与运营 JWT 隔离，互不踢下线。

## 范围

- 鉴权 / 多级部门 / 邀请码 / 建号
- 销售使用率大屏 + 人员明细（对话/外发/任务/群发等业务表）
- 人事账号管理（不看销售对话）

埋点（登录/商品复制/外呼点击）仍在核心 backend 后续穿插；未埋点前列为空属预期。