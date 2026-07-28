"""
Bot gửi báo cáo Google Sheet hàng ngày vào Telegram dưới dạng ảnh.

Cách hoạt động:
1. Tính ngày hôm nay theo giờ Việt Nam -> ghép thành tên tab dạng "BC ĐÓNG GÓI ddmmyy"
2. Ưu tiên tìm tab có hậu tố " BS" (bổ sung) trước, nếu không có thì dùng tab gốc
3. Dùng Service Account để lấy quyền, xuất tab đó thành PDF -> convert thành ảnh PNG
4. Gửi ảnh vào group Telegram

Các biến môi trường cần thiết (sẽ cấu hình trong GitHub Actions Secrets):
- TELEGRAM_BOT_TOKEN : token của bot Telegram
- TELEGRAM_CHAT_ID   : chat id của group (số âm)
- GOOGLE_CREDENTIALS_JSON : toàn bộ nội dung file JSON của service account (dạng text)
- SHEET_ID           : ID của Google Sheet (lấy từ URL sheet)
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from pdf2image import convert_from_bytes

# ---------- Cấu hình ----------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
TAB_PREFIX = "BC ĐÓNG GÓI"  # đổi nếu tên tab của bạn khác
VN_TZ = timezone(timedelta(hours=7))


def get_today_tab_name():
    """Trả về (tên_tab_uu_tien_BS, tên_tab_goc) theo ngày hôm nay giờ VN."""
    today = datetime.now(VN_TZ)
    ddmmyy = today.strftime("%d%m%y")
    base_name = f"{TAB_PREFIX} {ddmmyy}"
    bs_name = f"{base_name} BS"
    return bs_name, base_name


def load_credentials():
    raw = os.environ["GOOGLE_CREDENTIALS_JSON"]
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds


def find_sheet_gid(creds, sheet_id, candidate_names):
    """Tìm gid của tab khớp với danh sách tên ưu tiên (theo thứ tự)."""
    service = build("sheets", "v4", credentials=creds)
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets(properties(sheetId,title))"
    ).execute()

    title_to_gid = {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in meta.get("sheets", [])
    }

    for name in candidate_names:
        if name in title_to_gid:
            return name, title_to_gid[name]
    return None, None


def get_used_range_a1(creds, sheet_id, sheet_title):
    """Tính vùng có dữ liệu thực tế (VD: A1:F16) để cắt bỏ khoảng trắng thừa khi xuất ảnh."""
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_title}'!A1:Z300",
    ).execute()
    values = result.get("values", [])

    last_row = 0
    last_col = 0
    for r_idx, row in enumerate(values, start=1):
        for c_idx, cell in enumerate(row, start=1):
            if str(cell).strip() != "":
                last_row = max(last_row, r_idx)
                last_col = max(last_col, c_idx)

    if last_row == 0 or last_col == 0:
        return None  # không xác định được, xuất nguyên trang

    def col_letter(n):
        letters = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    return f"A1:{col_letter(last_col)}{last_row}"


def export_tab_as_png(creds, sheet_id, gid, a1_range=None):
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
        f"?format=pdf&gid={gid}"
        f"&portrait=false&fitw=true&gridlines=false"
        f"&top_margin=0&bottom_margin=0&left_margin=0&right_margin=0"
        f"&horizontal_alignment=CENTER&vertical_alignment=TOP"
    )
    if a1_range:
        export_url += f"&range={a1_range}"
    resp = requests.get(
        export_url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=60
    )
    resp.raise_for_status()

    images = convert_from_bytes(resp.content, dpi=200)
    if not images:
        raise RuntimeError("Không convert được PDF thành ảnh")

    out_path = "/tmp/report.png"
    images[0].save(out_path, "PNG")
    return out_path


def send_telegram_photo(image_path, caption):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with open(image_path, "rb") as photo:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": photo},
            timeout=60,
        )
    resp.raise_for_status()


def send_telegram_text(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    resp.raise_for_status()


def main():
    sheet_id = os.environ["SHEET_ID"]
    bs_name, base_name = get_today_tab_name()

    creds = load_credentials()
    matched_name, gid = find_sheet_gid(creds, sheet_id, [bs_name, base_name])

    if gid is None:
        msg = (
            f"⚠️ Không tìm thấy tab báo cáo hôm nay.\n"
            f"Đã tìm: \"{bs_name}\" và \"{base_name}\" nhưng không có trong sheet."
        )
        print(msg)
        send_telegram_text(msg)
        sys.exit(0)  # không coi là lỗi, có thể hôm nay không có báo cáo (cuối tuần...)

    a1_range = get_used_range_a1(creds, sheet_id, matched_name)
    image_path = export_tab_as_png(creds, sheet_id, gid, a1_range)
    caption = f"📊 {matched_name}"
    send_telegram_photo(image_path, caption)
    print(f"Đã gửi thành công tab: {matched_name}")


if __name__ == "__main__":
    main()
