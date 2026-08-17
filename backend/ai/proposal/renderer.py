from __future__ import annotations

import io
import os
from copy import copy
from pathlib import Path
from urllib.parse import urljoin

import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.drawing.spreadsheet_drawing import (
    AnchorMarker,
    OneCellAnchor,
    XDRPositiveSize2D,
)
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.units import pixels_to_EMU, points_to_pixels
from PIL import Image as PillowImage
from core.logger import logger


HEADERS = ["序号", "商品名称", "产品图片", "规格", "平台价格", "数量", "优惠单价", "合计", "优惠价合计", "备注"]
TEMPLATE_NAME = "proposal_template.xlsx"


def proposal_template_path() -> Path:
    configured = (os.getenv("PROPOSAL_TEMPLATE_PATH") or "").strip()
    if configured:
        return Path(configured)
    backend_dir = Path(__file__).resolve().parents[2]
    return backend_dir / "data" / TEMPLATE_NAME


def _template_is_usable(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "文件不存在"
    try:
        workbook = load_workbook(path, read_only=False, data_only=False)
        try:
            sheet = workbook.active
            actual = [
                sheet.cell(2, index).value for index in range(1, len(HEADERS) + 1)
            ]
            if actual != HEADERS:
                return False, f"第2行表头不匹配：{actual}"
            if sheet.max_row < 6:
                return False, "缺少样例数据行或合计区"
            return True, ""
        finally:
            workbook.close()
    except Exception as error:
        return False, str(error)


def _template_snapshots(sheet) -> tuple[list, list, list]:
    product_first = [copy(sheet.cell(3, column)._style) for column in range(1, 11)]
    product_next = [copy(sheet.cell(4, column)._style) for column in range(1, 11)]
    total = [
        [copy(sheet.cell(row, column)._style) for column in range(1, 11)]
        for row in (6, 7)
    ]
    return product_first, product_next, total


def _center_image_in_cell(sheet, image: ExcelImage, column: int, row: int) -> None:
    """使用带偏移量的单元格锚点，让图片在目标格内水平、垂直居中。"""
    letter = get_column_letter(column)
    column_width = sheet.column_dimensions[letter].width or 8.43
    row_height = sheet.row_dimensions[row].height or 15
    # Excel 列宽不是像素；常用字体下约为 width*7+5，行高则是磅值。
    cell_width_px = int(column_width * 7 + 5)
    cell_height_px = int(points_to_pixels(row_height))
    offset_x = max(0, (cell_width_px - int(image.width)) // 2)
    offset_y = max(0, (cell_height_px - int(image.height)) // 2)
    image.anchor = OneCellAnchor(
        _from=AnchorMarker(
            col=column - 1,
            row=row - 1,
            colOff=pixels_to_EMU(offset_x),
            rowOff=pixels_to_EMU(offset_y),
        ),
        ext=XDRPositiveSize2D(
            cx=pixels_to_EMU(int(image.width)),
            cy=pixels_to_EMU(int(image.height)),
        ),
    )
    sheet.add_image(image)


def _absolute_image_url(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("API_PUBLIC_BASE_URL") or "").strip()
    if base and value:
        return urljoin(base.rstrip("/") + "/", value.lstrip("/"))
    return value


async def _download_image(url: str) -> bytes | None:
    resolved = _absolute_image_url(url)
    if not resolved:
        return None
    if resolved.startswith("/media/"):
        backend_dir = Path(__file__).resolve().parents[2]
        local_path = backend_dir / resolved.lstrip("/")
        try:
            return local_path.read_bytes()
        except OSError:
            return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.get(resolved, follow_redirects=True)
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                return None
            return response.content
    except Exception:
        return None


def _placeholder_bytes() -> bytes:
    image = PillowImage.new("RGB", (240, 180), color=(242, 244, 247))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _normalized_png(raw: bytes | None) -> bytes:
    try:
        with PillowImage.open(io.BytesIO(raw or b"")) as image:
            image.thumbnail((360, 240))
            converted = image.convert("RGB")
            output = io.BytesIO()
            converted.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception:
        return _placeholder_bytes()


async def _render_template_xlsx(spec: dict, target: Path, template: Path) -> None:
    workbook = load_workbook(template, data_only=False)
    sheet = workbook.active
    sheet.title = "方案"
    product_first, product_next, total_styles = _template_snapshots(sheet)

    # 保存标题与表头，只重建动态商品行和合计区。模板中的产品图是 WPS DISPIMG
    # 私有公式，清除后改用标准 openpyxl drawing 图片。
    for merged in list(sheet.merged_cells.ranges):
        if merged.min_row >= 3:
            sheet.unmerge_cells(str(merged))
    if sheet.max_row >= 3:
        sheet.delete_rows(3, sheet.max_row - 2)

    image_buffers: list[io.BytesIO] = []
    lines = spec.get("lines") or []
    for offset, line in enumerate(lines):
        row_index = 3 + offset
        styles = product_first if offset == 0 else product_next
        values = [
            line.get("seq"),
            line.get("product_name"),
            "",
            line.get("spec"),
            line.get("platform_price"),
            line.get("qty"),
            line.get("promo_unit_price"),
            f"=F{row_index}*E{row_index}",
            f"=G{row_index}*F{row_index}",
            line.get("remark"),
        ]
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row_index, column, value)
            cell._style = copy(styles[column - 1])
            if column in (5, 7, 8, 9):
                cell.number_format = "¥0.00"
        sheet.row_dimensions[row_index].height = 85

        image_data = _normalized_png(
            await _download_image(str(line.get("image_url") or ""))
        )
        buffer = io.BytesIO(image_data)
        image_buffers.append(buffer)
        excel_image = ExcelImage(buffer)
        excel_image.width = 110
        excel_image.height = 76
        _center_image_in_cell(sheet, excel_image, 3, row_index)

    total_start = 3 + len(lines)
    total_end = total_start + 1
    for row_offset, row_index in enumerate((total_start, total_end)):
        for column in range(1, 11):
            sheet.cell(row_index, column)._style = copy(
                total_styles[row_offset][column - 1]
            )
    sheet.row_dimensions[total_start].height = 17.25
    sheet.row_dimensions[total_end].height = 17.25
    sheet.merge_cells(
        start_row=total_start, start_column=1, end_row=total_end, end_column=7
    )
    sheet.merge_cells(
        start_row=total_start, start_column=8, end_row=total_end, end_column=8
    )
    sheet.merge_cells(
        start_row=total_start, start_column=9, end_row=total_end, end_column=9
    )
    sheet.merge_cells(
        start_row=total_start, start_column=10, end_row=total_end, end_column=10
    )

    totals = spec.get("totals") or {}
    meta = spec.get("meta") or {}
    budget = float(meta.get("per_capita_budget") or 0)
    per_capita = float(totals.get("per_capita_promo") or 0)
    # 合计区不展示毛利率/折扣，避免出现在客户可见的 xlsx 中
    summary = (
        f"合计｜人均优惠价 ¥{per_capita:.2f}"
        + (f"（预算 ¥{budget:.2f}）" if budget > 0 else "")
    )
    sheet.cell(total_start, 1, summary)
    sheet.cell(total_start, 1).alignment = Alignment(
        horizontal="center", vertical="center", wrap_text=True
    )
    first_data_row = 3
    last_data_row = max(first_data_row, total_start - 1)
    sheet.cell(total_start, 8, f"=SUM(H{first_data_row}:H{last_data_row})")
    sheet.cell(total_start, 9, f"=SUM(I{first_data_row}:I{last_data_row})")
    sheet.cell(total_start, 10, "包邮")
    for column in (8, 9):
        sheet.cell(total_start, column).number_format = "¥0.00"

    sheet.sheet_view.showGridLines = False
    # sheet.freeze_panes = "A3"
    sheet.print_area = f"A1:J{total_end}"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    workbook.close()


async def _render_generated_xlsx(spec: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "方案"
    sheet.sheet_view.showGridLines = False

    sheet.merge_cells("A1:J1")
    title_cell = sheet["A1"]
    title_cell.value = spec.get("title") or "产品供应方案（包邮）"
    title_cell.font = Font(name="微软雅黑", size=18, bold=True)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    title_cell.fill = PatternFill("solid", fgColor="DDEBF7")
    sheet.row_dimensions[1].height = 34

    thin = Side(style="thin", color="808080")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for column, header in enumerate(HEADERS, 1):
        cell = sheet.cell(2, column, header)
        cell.font = Font(name="微软雅黑", bold=True, color="C00000" if "优惠" in header else "000000")
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    image_buffers: list[io.BytesIO] = []
    for row_index, line in enumerate(spec.get("lines") or [], 3):
        values = [
            line.get("seq"),
            line.get("product_name"),
            "",
            line.get("spec"),
            line.get("platform_price"),
            line.get("qty"),
            line.get("promo_unit_price"),
            line.get("platform_subtotal"),
            line.get("promo_subtotal"),
            line.get("remark"),
        ]
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row_index, column, value)
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if column in (7, 9):
                cell.font = Font(name="微软雅黑", color="C00000", bold=True)
            if column in (5, 7, 8, 9):
                cell.number_format = '¥0.00'
        sheet.row_dimensions[row_index].height = 92

        image_data = _normalized_png(await _download_image(str(line.get("image_url") or "")))
        buffer = io.BytesIO(image_data)
        image_buffers.append(buffer)
        excel_image = ExcelImage(buffer)
        excel_image.width = 110
        excel_image.height = 82
        _center_image_in_cell(sheet, excel_image, 3, row_index)

    total_row = 3 + len(spec.get("lines") or [])
    totals = spec.get("totals") or {}
    sheet.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=2)
    summary = [
        (1, "合计"),
        (3, "人均优惠价"),
        (4, totals.get("per_capita_promo", 0)),
        (5, "平台总价"),
        (6, totals.get("platform_total", 0)),
        (7, "优惠总价"),
        (9, totals.get("promo_total", 0)),
    ]
    for column, value in summary:
        cell = sheet.cell(total_row, column, value)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
        if column in (4, 9):
            cell.font = Font(name="微软雅黑", color="C00000", bold=True, size=12)
            cell.number_format = '¥0.00'
    for column in range(1, 11):
        sheet.cell(total_row, column).border = border
    sheet.merge_cells(start_row=total_row, start_column=7, end_row=total_row, end_column=8)
    sheet.merge_cells(start_row=total_row, start_column=9, end_row=total_row, end_column=10)
    sheet.row_dimensions[total_row].height = 30

    widths = [8, 30, 18, 18, 14, 10, 14, 14, 16, 24]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    # sheet.freeze_panes = "A3"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    workbook.save(target)
    workbook.close()


async def render_xlsx(spec: dict, target: Path) -> None:
    template = proposal_template_path()
    usable, reason = _template_is_usable(template)
    if usable:
        try:
            await _render_template_xlsx(spec, target, template)
            logger.info("方案 Excel 已使用模板 path={}", template)
            return
        except Exception as error:
            logger.exception(
                "方案模板渲染失败，回退代码生成 path={} error={}", template, error
            )
    else:
        logger.warning("方案模板不可用，回退代码生成 path={} reason={}", template, reason)
    await _render_generated_xlsx(spec, target)

