"""Feishu (Lark) spreadsheet export client.

Wraps the three calls the export needs:
1. tenant_access_token (cached, refreshed before expiry)
2. spreadsheet creation (once, token persisted in app_config)
3. values_append to append one row per analysis

Requires a Feishu self-built app with the `sheets:spreadsheet` permission
(读写电子表格). Configure via .env:

    FEISHU_APP_ID=cli_xxxxxxxx
    FEISHU_APP_SECRET=xxxxxxxx
    FEISHU_SPREADSHEET_TOKEN=  # optional; auto-created on first export when empty
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import settings
from .db import get_config, set_config

API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{API_BASE}/auth/v3/tenant_access_token/internal"
CREATE_SHEET_URL = f"{API_BASE}/sheets/v3/spreadsheets"
SHEETS_QUERY_URL = f"{API_BASE}/sheets/v3/spreadsheets/{{token}}/sheets/query"
APPEND_URL = f"{API_BASE}/sheets/v2/spreadsheets/{{token}}/values_append"
VALUES_URL = f"{API_BASE}/sheets/v2/spreadsheets/{{token}}/values"
VALUES_RANGE_URL = f"{API_BASE}/sheets/v2/spreadsheets/{{token}}/values/{{range}}"
INSERT_IMAGE_URL = f"{API_BASE}/sheets/v2/spreadsheets/{{token}}/values_image"
STYLE_URL = f"{API_BASE}/sheets/v2/spreadsheets/{{token}}/style"
DIMENSION_URL = f"{API_BASE}/sheets/v2/spreadsheets/{{token}}/dimension_range"
TRANSFER_OWNER_URL = f"{API_BASE}/drive/v1/permissions/{{token}}/members/transfer_owner"

_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}


class FeishuError(RuntimeError):
    pass


def _check(data: dict[str, Any]) -> None:
    if data.get("code", 0) != 0:
        raise FeishuError(f"飞书 API 错误 {data.get('code')}: {data.get('msg')}")


async def _request(method: str, url: str, *, headers: dict[str, str] | None = None,
                   json: dict[str, Any] | None = None,
                   files: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.request(method, url, headers=headers, json=json, files=files)
        response.raise_for_status()
        data = response.json()
    _check(data)
    return data


async def get_tenant_access_token() -> str:
    """Return a cached tenant_access_token, refreshing it before expiry."""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 300:
        return _token_cache["token"]
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise FeishuError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，请在 .env 中填写飞书自建应用凭证")
    data = await _request("POST", TOKEN_URL, json={
        "app_id": settings.feishu_app_id,
        "app_secret": settings.feishu_app_secret,
    })
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + float(data.get("expire", 7200))
    return _token_cache["token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _create_spreadsheet(token: str, title: str) -> tuple[str, str, str]:
    """Create a spreadsheet, return (spreadsheet_token, sheet_id, sheet_title)."""
    data = await _request("POST", CREATE_SHEET_URL, headers=_auth_headers(token), json={"title": title})
    spreadsheet = data["data"]["spreadsheet"]
    spreadsheet_token = spreadsheet["spreadsheet_token"]
    return spreadsheet_token, *await _first_sheet(token, spreadsheet_token)


async def _first_sheet(token: str, spreadsheet_token: str) -> tuple[str, str]:
    data = await _request("GET", SHEETS_QUERY_URL.format(token=spreadsheet_token), headers=_auth_headers(token))
    sheet = data["data"]["sheets"][0]
    return sheet["sheet_id"], sheet.get("title", "Sheet1")


async def append_rows(token: str, spreadsheet_token: str, sheet_id: str, rows: list[list[str]]) -> None:
    url = APPEND_URL.format(token=spreadsheet_token)
    # values_append 的 range 必须用 sheet_id 前缀（标题可能不被识别），且覆盖待写入的行数（含表头）
    range_end = f"A1:Z{max(len(rows), 1)}"
    payload = {"valueRange": {"range": f"{sheet_id}!{range_end}", "values": rows}}
    await _request("POST", url, headers=_auth_headers(token), json=payload)


async def overwrite_rows(token: str, spreadsheet_token: str, sheet_id: str,
                         rows: list[list[str]], clear_rows: int = 200) -> None:
    """Overwrite the sheet from A1 with the given rows.

    values_append appends after the sheet's current used range, which shifts
    every time and misaligns images; overwriting from A1 keeps the table as an
    exact snapshot of the exported data. `clear_rows` wipes the rest of the old
    range so stale rows from previous exports do not linger below the new data.
    """
    # 1. 清空旧范围（从 A1 到 clear_rows 行），避免残留旧数据
    clear_url = VALUES_URL.format(token=spreadsheet_token)
    clear_payload = {"valueRange": {
        "range": f"{sheet_id}!A1:Z{clear_rows}",
        "values": [[""] * 26 for _ in range(clear_rows)],
    }}
    await _request("PUT", clear_url, headers=_auth_headers(token), json=clear_payload)
    # 2. 覆盖写新数据
    await _request("PUT", clear_url, headers=_auth_headers(token), json={
        "valueRange": {"range": f"{sheet_id}!A1:Z{len(rows)}", "values": rows},
    })


async def read_used_row_count(token: str, spreadsheet_token: str, sheet_id: str,
                              max_rows: int = 500) -> int:
    """Return how many rows currently hold data in column A (1-based count).

    Used to decide where the next auto-appended row goes without disturbing
    existing rows. Reads A1:A{max_rows} and counts non-empty cells (None and
    empty strings are both treated as empty).
    """
    url = VALUES_RANGE_URL.format(token=spreadsheet_token, range=f"{sheet_id}!A1:A{max_rows}")
    data = await _request("GET", url, headers=_auth_headers(token))
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    count = 0
    for row in values:
        cell = row[0] if row else None
        if isinstance(cell, str) and cell.strip():
            count += 1
    return count


async def write_row_at(token: str, spreadsheet_token: str, sheet_id: str,
                       row_number: int, values: list[str]) -> None:
    """Write one full data row at a 1-based row number via values PUT."""
    col_letter = "Z" if len(values) > 26 else chr(ord("A") + max(len(values) - 1, 0))
    url = VALUES_URL.format(token=spreadsheet_token)
    payload = {"valueRange": {
        "range": f"{sheet_id}!A{row_number}:{col_letter}{row_number}",
        "values": [values],
    }}
    await _request("PUT", url, headers=_auth_headers(token), json=payload)


async def ensure_header(token: str, spreadsheet_token: str, sheet_id: str,
                        header: list[str]) -> None:
    """Write the header row if row 1 is empty; no-op otherwise."""
    count = await read_used_row_count(token, spreadsheet_token, sheet_id, max_rows=1)
    if count == 0:
        await write_row_at(token, spreadsheet_token, sheet_id, 1, header)


async def insert_image(token: str, spreadsheet_token: str, sheet_id: str,
                       row_index: int, column_index: int, image_bytes: bytes,
                       content_type: str = "image/jpeg") -> None:
    """Write an image into a single cell (0-based row/column).

    Uses the official values_image API: the image binary is base64-encoded in
    the JSON body. range uses the sheet_id prefix, e.g. '9e9aed!B2:B2'.
    """
    import base64
    url = INSERT_IMAGE_URL.format(token=spreadsheet_token)
    cell = f"{chr(ord('A') + column_index)}{row_index + 1}"
    suffix = "png" if "png" in content_type else "jpg"
    payload = {
        "range": f"{sheet_id}!{cell}:{cell}",
        "image": base64.b64encode(image_bytes).decode("ascii"),
        "name": f"cover.{suffix}",
    }
    await _request("POST", url, headers=_auth_headers(token), json=payload)


async def apply_style(token: str, spreadsheet_token: str, sheet_id: str,
                      range_expr: str, style: dict[str, Any]) -> None:
    """Set cell styles (bold, background color, alignment, wrap, etc.) via the style API."""
    url = STYLE_URL.format(token=spreadsheet_token)
    payload = {"appendStyle": {"range": f"{sheet_id}!{range_expr}", "style": style}}
    await _request("PUT", url, headers=_auth_headers(token), json=payload)


async def set_column_widths(token: str, spreadsheet_token: str, sheet_id: str,
                            widths: list[int]) -> None:
    """Set column widths (px) so long text wraps and reads comfortably.

    Feishu cells wrap by default; sensible column widths + auto row height
    (rows keep their default auto height since we never pin fixedSize) make the
    content fully visible.
    """
    url = DIMENSION_URL.format(token=spreadsheet_token)
    for index, width in enumerate(widths):
        payload = {
            "dimension": {
                "sheetId": sheet_id,
                "majorDimension": "COLUMNS",
                "startIndex": index + 1,
                "endIndex": index + 1,
            },
            "dimensionProperties": {"visible": True, "fixedSize": width},
        }
        await _request("PUT", url, headers=_auth_headers(token), json=payload)


HEADER_STYLE = {
    "font": {"bold": True, "fontSize": "10pt/1.5"},
    "backColor": "#FFE699",
    "hAlign": 1,
    "vAlign": 1,
    "clean": False,
}
DATA_STYLE = {
    "font": {"fontSize": "9pt/1.5"},
    "hAlign": 0,
    "vAlign": 0,
    "clean": False,
}
# 末尾第 13 列（M）为"分析人"：云端多用户共享一张表时用于归属每条分析
COLUMN_WIDTHS = [60, 110, 140, 140, 320, 320, 320, 60, 80, 90, 80, 90, 90]
EXPORT_HEADER = ["平台", "视频关键帧截图", "视频标题", "首帧标题", "视频文案", "文案结构",
                 "提取起量点", "是否可借鉴", "项目", "素材命名", "成片", "数据情况", "分析人"]


def analysis_to_row(item: dict[str, Any]) -> list[str]:
    """Build one spreadsheet row from a completed analysis dict."""
    return [
        str(item.get("platform") or ""),
        "",  # 封面图通过 insert_image 插入
        str(item.get("title") or ""),
        str(item.get("video_title") or ""),
        str(item.get("transcript") or ""),
        str(item.get("copy_structure") or ""),
        str(item.get("boost_points") or ""),
        "", "", "", "", "",
        str(item.get("username") or ""),  # 分析人（来自 ia.user_id JOIN users.username）
    ]


def export_widths() -> list[int]:
    """Return the tuned column widths for the export table."""
    return list(COLUMN_WIDTHS)


async def style_row(token: str, spreadsheet_token: str, sheet_id: str,
                    row_number: int, *, header: bool = False) -> None:
    """Apply header (yellow bold) or data style to one row."""
    style = HEADER_STYLE if header else DATA_STYLE
    await apply_style(token, spreadsheet_token, sheet_id, f"A{row_number}:M{row_number}", style)


async def style_export(token: str, spreadsheet_token: str, sheet_id: str,
                       data_rows: int) -> None:
    """Apply formatting to the exported table.

    - Header row: yellow background (#FFE699), bold, centered.
    - Data rows: top-left aligned with wrap enabled (Feishu wraps by default;
      we set vAlign/hAlign so long text reads naturally).
    - Column widths tuned so the long text columns wrap into multiple lines.
    """
    await style_row(token, spreadsheet_token, sheet_id, 1, header=True)
    if data_rows > 1:
        await apply_style(token, spreadsheet_token, sheet_id, f"A2:M{data_rows}", DATA_STYLE)
    await set_column_widths(token, spreadsheet_token, sheet_id, COLUMN_WIDTHS)


async def transfer_owner(token: str, spreadsheet_token: str) -> None:
    """Transfer the spreadsheet's owner to the configured user openid.

    Spreadsheets created with the app identity belong to the app (bot); this
    hands full ownership to the human operator so they can edit/manage it in
    the Feishu UI. Configure FEISHU_OWNER_OPENID in .env; skipped when empty.
    """
    if not settings.feishu_owner_openid:
        return
    url = f"{TRANSFER_OWNER_URL}?type=sheet"
    payload = {"member_type": "openid", "member_id": settings.feishu_owner_openid}
    await _request("POST", url.format(token=spreadsheet_token), headers=_auth_headers(token), json=payload)


async def ensure_spreadsheet(token: str) -> tuple[str, str, str]:
    """Return (spreadsheet_token, sheet_id, sheet_title); auto-create on first export.

    When a new spreadsheet is auto-created, ownership is transferred to the
    configured human operator (FEISHU_OWNER_OPENID) so the app's bot identity
    does not keep exclusive control of the document.
    """
    spreadsheet_token = settings.feishu_spreadsheet_token or get_config("feishu_spreadsheet_token") or ""
    if spreadsheet_token:
        try:
            sheet_id, sheet_title = await _first_sheet(token, spreadsheet_token)
            return spreadsheet_token, sheet_id, sheet_title
        except FeishuError:
            # Token may point to a deleted spreadsheet; fall through and create a new one.
            spreadsheet_token = ""
    spreadsheet_token, sheet_id, sheet_title = await _create_spreadsheet(token, "灵感分析")
    set_config("feishu_spreadsheet_token", spreadsheet_token)
    try:
        await transfer_owner(token, spreadsheet_token)
    except Exception:
        # Ownership handover is best-effort; export must still succeed.
        pass
    return spreadsheet_token, sheet_id, sheet_title


async def export_rows(rows: list[list[str]]) -> dict[str, Any]:
    """Append rows (list of cell-string lists) to the Feishu spreadsheet.

    First row is treated as the header when the spreadsheet is freshly created;
    we always write the header + data so a new sheet starts with headers.
    """
    if not rows:
        raise FeishuError("没有可导出的数据")
    token = await get_tenant_access_token()
    spreadsheet_token, sheet_id, _sheet_title = await ensure_spreadsheet(token)
    await append_rows(token, spreadsheet_token, sheet_id, rows)
    return {"spreadsheet_token": spreadsheet_token, "rows": len(rows)}
