"""管理后台：营销活动 CRUD + 多海报上传。"""
from __future__ import annotations

import datetime
from typing import Any

from markupsafe import Markup
from sqladmin import BaseView, expose
from sqlalchemy import select
from starlette.requests import Request
from wtforms import SelectField, TextAreaField
from wtforms.validators import InputRequired, NumberRange, Optional as WTFOptional

from ai.campaign_service import (
    AUDIENCE_GENERAL,
    STATUS_DISABLED,
    STATUS_ENABLED,
    audience_choices_from_config,
    normalize_audience,
)
from core.admin_pages import render_admin_page
from core.admin_sort import AdminModelView
from core.campaign_media import (
    CampaignMediaError,
    delete_campaign_dir,
    delete_poster_file,
    save_campaign_poster,
)
from core.upload_limits import UploadLimitError, read_capped_upload
from database import AsyncSessionLocal
from models import Campaign, CampaignPoster


def _effective_status(model: Campaign) -> str:
    now = datetime.datetime.now()
    if (model.status or "") != STATUS_ENABLED:
        return "已关闭"
    if model.start_at and now < model.start_at:
        return "未开始"
    if model.end_at and now > model.end_at:
        return "已结束"
    return "进行中"


def _audience_label(model: Campaign) -> str:
    types = normalize_audience(model.audience_unit_types)
    return "、".join(types) if types else "—"


class CampaignAdmin(AdminModelView, model=Campaign):
    category = "营销策略管理"
    name = "营销活动"
    name_plural = "营销活动"
    page_size = 50

    column_list = [
        Campaign.id,
        Campaign.name,
        Campaign.start_at,
        Campaign.end_at,
        "audience_label",
        Campaign.status,
        "effective_status",
        Campaign.priority,
        "posters_link",
    ]
    column_searchable_list = [Campaign.name]
    column_sortable_list = [
        Campaign.id,
        Campaign.start_at,
        Campaign.end_at,
        Campaign.status,
        Campaign.priority,
    ]
    column_default_sort = [(Campaign.id, True)]
    column_labels = {
        Campaign.id: "ID",
        Campaign.name: "活动名称",
        Campaign.start_at: "开始时间",
        Campaign.end_at: "结束时间",
        Campaign.audience_unit_types: "面向客户类型",
        Campaign.rules: "活动规则",
        Campaign.status: "活动状态",
        Campaign.priority: "优先级（大优先）",
        Campaign.created_at: "创建时间",
        Campaign.updated_at: "更新时间",
        "audience_label": "面向客户类型",
        "effective_status": "当前是否生效",
        "posters_link": "海报",
    }
    form_excluded_columns = [
        "posters",
        "poster_sends",
        "created_at",
        "updated_at",
        "audience_unit_types",
    ]
    form_overrides = {
        "status": SelectField,
        "rules": TextAreaField,
    }
    form_args = {
        "name": {"label": "活动名称", "validators": [InputRequired()]},
        "start_at": {
            "label": "开始时间",
            "description": "到达该时刻后，在状态为开启时开始对匹配客户注入。",
        },
        "end_at": {
            "label": "结束时间",
            "description": "超过该时刻即不再注入；建议填当天 23:59。",
        },
        "rules": {
            "label": "活动规则",
            "description": "给 AI / 销售的玩法说明（力度、门槛、禁说口径）。不要当微信原文整段发出。",
            "render_kw": {"rows": 10, "class": "form-control"},
            "validators": [WTFOptional()],
        },
        "status": {
            "label": "活动状态",
            "choices": [(STATUS_ENABLED, "开启"), (STATUS_DISABLED, "关闭")],
            "description": "关闭后即使仍在时间窗口内也不会出现在对话里。",
        },
        "priority": {
            "label": "优先级",
            "description": "同一客户命中多场专项时，数字更大的优先。",
            "validators": [WTFOptional(), NumberRange(min=-100, max=1000)],
        },
    }
    column_formatters = {
        Campaign.status: lambda m, a: "开启" if (m.status or "") == STATUS_ENABLED else "关闭",
        "audience_label": lambda m, a: _audience_label(m),
        "effective_status": lambda m, a: _effective_status(m),
        "posters_link": lambda m, a: Markup(
            f'<a href="/admin/campaign-posters?pk={int(m.id)}">管理海报</a>'
        ),
        Campaign.rules: lambda m, a: ((m.rules or "")[:40] + "…")
        if m.rules and len(m.rules) > 40
        else (m.rules or ""),
    }

    async def scaffold_form(self, rules=None):
        from admin_views import MultiCheckboxField

        form_class = await super().scaffold_form(rules)
        async with AsyncSessionLocal() as db:
            choices = await audience_choices_from_config(db)

        class ExtendedForm(form_class):  # type: ignore[misc, valid-type]
            audience_unit_types_field = MultiCheckboxField(
                label="面向客户类型",
                choices=choices,
                validators=[InputRequired(message="请至少选择一种面向客户类型")],
                description=(
                    "勾选「通用」则所有客户可见；只勾具体单位则仅该类客户可见。"
                    "有专项活动时不会再推通用场。"
                ),
            )

            def process(self, formdata=None, obj=None, data=None, **kwargs):
                super().process(formdata, obj, data=data, **kwargs)
                if formdata is not None:
                    return
                if obj is None:
                    self.audience_unit_types_field.data = [AUDIENCE_GENERAL]
                    return
                selected = normalize_audience(getattr(obj, "audience_unit_types", None))
                existing = {str(c[0]) for c in (self.audience_unit_types_field.choices or [])}
                extra = [(n, n) for n in selected if n not in existing]
                if extra:
                    self.audience_unit_types_field.choices = list(
                        self.audience_unit_types_field.choices or []
                    ) + extra
                self.audience_unit_types_field.data = selected or [AUDIENCE_GENERAL]

        return ExtendedForm

    async def on_model_change(self, data: dict, model: Campaign, is_created: bool, request: Any) -> None:
        selected = normalize_audience(data.pop("audience_unit_types_field", None))
        if not selected:
            selected = [AUDIENCE_GENERAL]
        if AUDIENCE_GENERAL in selected and len(selected) > 1:
            # 勾了通用再勾单位没有额外效果，保存为通用即可
            selected = [AUDIENCE_GENERAL]
        data["audience_unit_types"] = selected
        model.audience_unit_types = list(selected)
        status = (data.get("status") or STATUS_ENABLED).strip()
        if status not in (STATUS_ENABLED, STATUS_DISABLED):
            data["status"] = STATUS_ENABLED

    async def on_model_delete(self, model: Campaign, request: Any) -> None:
        delete_campaign_dir(int(model.id))


class CampaignPosterView(BaseView):
    """活动海报上传页：文件落在 media/campaigns/{id}/，库里只存路径。"""

    name = "活动海报"
    category = "营销策略管理"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/campaign-posters", methods=["GET", "POST"], identity="campaign_posters")
    async def manage_posters(self, request: Request):
        pk_raw = (request.query_params.get("pk") or "").strip()
        try:
            pk = int(pk_raw)
        except (TypeError, ValueError):
            pk = 0
        message = ""
        error = False

        async with AsyncSessionLocal() as db:
            camp = await db.get(Campaign, pk) if pk else None
            if camp is None:
                return await render_admin_page(
                    request,
                    "admin/campaign_posters.html",
                    title="活动海报",
                    subtitle="营销活动",
                    campaign=None,
                    posters=[],
                    message="未找到该活动",
                    error=True,
                )

            if request.method == "POST":
                form = await request.form()
                action = str(form.get("action") or "upload").strip()
                try:
                    if action == "upload":
                        uploads = form.getlist("files") if hasattr(form, "getlist") else [form.get("files")]
                        saved = 0
                        max_order_res = await db.execute(
                            select(CampaignPoster.sort_order)
                            .where(CampaignPoster.campaign_id == camp.id)
                            .order_by(CampaignPoster.sort_order.desc())
                            .limit(1)
                        )
                        next_order = int(max_order_res.scalar() or 0) + 1
                        for upload in uploads:
                            filename = str(getattr(upload, "filename", "") or "")
                            if not filename:
                                continue
                            content = await read_capped_upload(upload)
                            rel = save_campaign_poster(int(camp.id), filename, content)
                            db.add(
                                CampaignPoster(
                                    campaign_id=int(camp.id),
                                    image_path=rel,
                                    sort_order=next_order,
                                    is_active=True,
                                )
                            )
                            next_order += 1
                            saved += 1
                        if saved == 0:
                            message = "请选择至少一张图片"
                            error = True
                        else:
                            await db.commit()
                            message = f"已上传 {saved} 张海报"
                    elif action in ("delete", "toggle", "up", "down"):
                        poster_id = int(str(form.get("poster_id") or "0"))
                        poster = await db.get(CampaignPoster, poster_id)
                        if not poster or int(poster.campaign_id) != int(camp.id):
                            message = "海报不存在"
                            error = True
                        elif action == "delete":
                            delete_poster_file(poster.image_path)
                            await db.delete(poster)
                            await db.commit()
                            message = "已删除海报"
                        elif action == "toggle":
                            poster.is_active = not bool(poster.is_active)
                            await db.commit()
                            message = "已启用海报" if poster.is_active else "已停用海报"
                        else:
                            posters_res = await db.execute(
                                select(CampaignPoster)
                                .where(CampaignPoster.campaign_id == camp.id)
                                .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
                            )
                            rows = list(posters_res.scalars().all())
                            idx = next((i for i, r in enumerate(rows) if r.id == poster.id), -1)
                            swap_with = idx - 1 if action == "up" else idx + 1
                            if idx < 0 or swap_with < 0 or swap_with >= len(rows):
                                message = "已到边界，无法再移动"
                                error = True
                            else:
                                rows[idx].sort_order, rows[swap_with].sort_order = (
                                    rows[swap_with].sort_order,
                                    rows[idx].sort_order,
                                )
                                await db.commit()
                                message = "已调整顺序"
                except UploadLimitError as exc:
                    await db.rollback()
                    message = str(exc)
                    error = True
                except CampaignMediaError as exc:
                    await db.rollback()
                    message = str(exc)
                    error = True
                except Exception as exc:
                    await db.rollback()
                    message = f"操作失败：{exc}"
                    error = True

            posters_res = await db.execute(
                select(CampaignPoster)
                .where(CampaignPoster.campaign_id == camp.id)
                .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
            )
            posters = list(posters_res.scalars().all())

        return await render_admin_page(
            request,
            "admin/campaign_posters.html",
            title=f"海报 · {camp.name}",
            subtitle="营销活动",
            campaign=camp,
            posters=posters,
            message=message,
            error=error,
        )
