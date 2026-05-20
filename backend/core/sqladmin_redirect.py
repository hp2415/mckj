"""sqladmin：保存后与中文按钮匹配的跳转逻辑。

默认库的 get_save_redirect_url 只匹配英文 Submit 文案（Save 等），
模板改为中文后表单字段 save 永远不命中，会持续落到「新建」页。

在未选「继续编辑 / 再来一条」等时：
- 优先使用表单隐藏的 `next`（由进入新建/编辑页时的 document.referrer 写入），经服务端同源与 /admin 路径校验后用 302 GET 打开，
  从而刷新列表等业务页（不是 history.back）。
- `next` 非法或为空时回退到当前模型的列表页。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from sqladmin import Admin
from sqladmin.authentication import login_required
from sqladmin.helpers import get_object_identifier
from sqladmin.models import ModelView
from starlette.datastructures import URL
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

logger = logging.getLogger(__name__)


def sanitize_admin_next_url(
    raw: str | None,
    request: Request,
    admin_prefix: str = "/admin",
) -> URL | None:
    """只接受同源、且路径挂在管理前缀下的跳转目标。"""
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s.startswith("//"):
        return None

    parsed = urlparse(s)
    host = (parsed.hostname or "").lower()
    req_host = (request.url.hostname or "").lower()

    path = parsed.path or "/"
    if ".." in path:
        return None

    prefix = admin_prefix.rstrip("/") or "/admin"
    if path != prefix and not path.startswith(prefix + "/"):
        return None

    # 若是绝对 URL，须与当前请求的 host 一致
    if host and host != req_host:
        return None

    # 同源相对路径时没有 netloc / hostname
    query = parsed.query or ""
    return URL(str(request.url.replace(path=path, query=query, fragment="")))


class AdminWithReturnRedirect(Admin):
    """与中文保存按钮、`next` 回头页对齐的 Admin（其余行为与 sqladmin.Admin 一致）。"""

    def get_save_redirect_url(
        self,
        request: Request,
        form: Any,
        model_view: ModelView,
        obj: Any,
    ) -> str | URL:
        identity = request.path_params["identity"]
        identifier = get_object_identifier(obj)
        admin_prefix = self.base_url or "/admin"
        save = (form.get("save") if hasattr(form, "get") else None) or ""

        # 「继续在当前记录上改」（或新建后跳到编辑详情）
        if (
            save
            in {
                "保存并继续编辑",
                "创建并继续完善",
                "Save and continue editing",
            }
            or (
                save in {"作为新项保存", "Save as new"}
                and model_view.save_as_continue
            )
        ):
            return request.url_for("admin:edit", identity=identity, pk=identifier)

        # 「保存/创建后再开一条」（作为新项且 save_as_continue=False 时也走新建）
        if save in {
            "保存并新增下一条",
            "创建并新增下一条",
            "Save and add another",
        } or (
            save in {"作为新项保存", "Save as new"}
            and not model_view.save_as_continue
        ):
            return request.url_for("admin:create", identity=identity)

        # 「主要」保存：即时保存 / 即时创建 —— 以及英文 Submit 名 Save
        if save in {"立即保存", "立即创建", "Save"}:
            nu = sanitize_admin_next_url(
                form.get("next") if hasattr(form, "get") else None,
                request,
                admin_prefix=admin_prefix,
            )
            if nu is not None:
                return nu
            return request.url_for("admin:list", identity=identity)

        # 兜底：不把未知按钮再踢到新建（旧库行为）；优先 next，否则列表
        nu = sanitize_admin_next_url(
            form.get("next") if hasattr(form, "get") else None,
            request,
            admin_prefix=admin_prefix,
        )
        if nu is not None:
            return nu
        return request.url_for("admin:list", identity=identity)

    @login_required
    async def edit(self, request: Request) -> Response:
        """与库一致；补充中文「作为新项保存」以触发 insert_model（库内只认英文 Save as new）。"""
        await self._edit(request)

        identity = request.path_params["identity"]
        model_view = self._find_model_view(identity)

        model = await model_view.get_object_for_edit(request)
        if not model:
            raise HTTPException(status_code=404)

        Form = await model_view.scaffold_form(model_view._form_edit_rules)
        context: dict[str, Any] = {
            "obj": model,
            "model_view": model_view,
            "form": Form(obj=model, data=self._normalize_wtform_data(model)),
        }

        if request.method == "GET":
            return await self.templates.TemplateResponse(
                request, model_view.edit_template, context
            )

        form_data = await self._handle_form_data(request, model)
        form = Form(form_data)
        if not form.validate():
            context["form"] = form
            return await self.templates.TemplateResponse(
                request, model_view.edit_template, context, status_code=400
            )

        form_data_dict = self._denormalize_wtform_data(form.data, model)
        try:
            save_action = form_data.get("save")
            if model_view.save_as and save_action in ("Save as new", "作为新项保存"):
                obj = await model_view.insert_model(request, form_data_dict)
            else:
                obj = await model_view.update_model(
                    request,
                    pk=request.path_params["pk"],
                    data=form_data_dict,
                )
        except Exception as e:  # pragma: no cover - sqladmin parity
            logger.exception(e)
            context["error"] = str(e)
            return await self.templates.TemplateResponse(
                request, model_view.edit_template, context, status_code=400
            )

        url = self.get_save_redirect_url(
            request=request,
            form=form_data,
            obj=obj,
            model_view=model_view,
        )
        return RedirectResponse(url=url, status_code=302)
