"""
Bot gửi báo cáo Google Sheet hàng ngày vào Telegram dưới dạng ảnh + số liệu tổng hợp.

Cách hoạt động:
1. Báo cáo của ngày X được nhập vào sheet vào ngày X+1 -> bot luôn tìm tab của HÔM QUA
2. Ưu tiên tìm tab có hậu tố " BS" (bổ sung) trước, nếu không có thì dùng tab gốc
3. Đọc dữ liệu bảng, tính: tổng kế hoạch/thực tế/%, số mã hàng, ghi chú đặc biệt,
   so sánh với báo cáo lần trước (lưu trong state/last_sent.txt)
4. Xuất tab thành ảnh (đúng vùng dữ liệu, không dư khoảng trắng) -> gửi Telegram kèm caption

Các biến môi trường cần thiết (cấu hình trong GitHub Actions Secrets):
- TELEGRAM_BOT_TOKEN : token của bot Telegram
- TELEGRAM_CHAT_ID   : chat id của group (số âm)
- GOOGLE_CREDENTIALS_JSON : toàn bộ nội dung file JSON của service account (dạng text)
- SHEET_ID           : ID của Google Sheet (lấy từ URL sheet)
"""

import os
import sys
import json
import re
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
STATE_FILE = "state/last_sent.txt"
LOOKBACK_DAYS = 5  # số ngày tối đa để bot tự "quét bù" nếu bị bỏ lỡ (cron lỗi, nghỉ lễ...)


def load_state():
    """Đọc trạng thái: {"sent": {"ddmmyy": total_actual, ...}}"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {"sent": {}}
            data = json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}}

    if "sent" in data:
        return data

    # Định dạng cũ (chỉ lưu 1 tab gần nhất) -> chuyển đổi cho tương thích ngược
    sent = {}
    m = re.search(r"(\d{6})", data.get("tab", ""))
    if m and data.get("total_actual") is not None:
        sent[m.group(1)] = data["total_actual"]
    return {"sent": sent}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    sent = state.get("sent", {})
    if len(sent) > 30:  # chỉ giữ 30 ngày gần nhất, tránh file phình to
        sorted_items = sorted(sent.items(), key=lambda kv: datetime.strptime(kv[0], "%d%m%y"))
        sent = dict(sorted_items[-30:])
        state["sent"] = sent
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


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


def fetch_sheet_values(creds, sheet_id, sheet_title):
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_title}'!A1:Z300",
    ).execute()
    return result.get("values", [])


def get_used_range_a1(values):
    """Tính vùng có dữ liệu thực tế (VD: A1:F16) để cắt bỏ khoảng trắng thừa khi xuất ảnh."""
    last_row = 0
    last_col = 0
    for r_idx, row in enumerate(values, start=1):
        for c_idx, cell in enumerate(row, start=1):
            if str(cell).strip() != "":
                last_row = max(last_row, r_idx)
                last_col = max(last_col, c_idx)

    if last_row == 0 or last_col == 0:
        return None

    def col_letter(n):
        letters = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    return f"A1:{col_letter(last_col)}{last_row}"


def _to_number(text):
    text = text.replace(",", "").replace(".", "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def parse_report_metrics(values):
    """Đọc bảng báo cáo, trả về dict số liệu tổng hợp (hoặc None nếu không nhận diện được)."""
    header_idx = None
    col_idx = {}
    for i, row in enumerate(values):
        cells = [str(c).strip() for c in row]
        if "Kế hoạch" in cells and "Thực tế" in cells:
            header_idx = i
            for j, cell in enumerate(cells):
                col_idx[cell] = j
            break

    if header_idx is None:
        return None

    def get_cell(row, key):
        idx = col_idx.get(key)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    product_rows = []
    total_row = None
    for row in values[header_idx + 1:]:
        if not row:
            continue
        first_nonempty = next((str(c).strip() for c in row if str(c).strip()), "")
        if first_nonempty.upper() == "TỔNG":
            total_row = row
            break
        product_rows.append(row)

    total_plan = _to_number(get_cell(total_row, "Kế hoạch")) if total_row else None
    total_actual = _to_number(get_cell(total_row, "Thực tế")) if total_row else None

    percent = None
    if total_plan and total_actual is not None and total_plan != 0:
        percent = round(total_actual / total_plan * 100, 1)

    so_ma_hang = sum(1 for r in product_rows if get_cell(r, "Tên nệm") != "")

    notes = []
    for r in product_rows:
        note = get_cell(r, "Ghi chú")
        if note:
            ten = get_cell(r, "Tên nệm")
            kich_thuoc = get_cell(r, "Kích thước")
            label = " ".join(x for x in [ten, kich_thuoc] if x)
            notes.append(f"{label} – {note}" if label else note)

    return {
        "total_plan": total_plan,
        "total_actual": total_actual,
        "percent": percent,
        "so_ma_hang": so_ma_hang,
        "notes": notes,
    }


def build_caption(matched_name, metrics, prev_total_actual):
    lines = [f"📊 {matched_name}", ""]

    if not metrics:
        return lines[0]  # không đọc được bảng -> chỉ hiện tên tab

    tp = metrics["total_plan"]
    ta = metrics["total_actual"]
    pct = metrics["percent"]

    if tp is not None and ta is not None:
        pct_text = f" ({pct}%)" if pct is not None else ""
        lines.append(f"📈 Tổng: Kế hoạch {tp} | Thực tế {ta}{pct_text}")

    lines.append(f"🏷️ Số mã hàng: {metrics['so_ma_hang']} dòng sản phẩm")

    if pct is not None:
        if pct >= 100:
            lines.append(f"✅ Hoàn thành {pct}% kế hoạch")
        else:
            lines.append(f"⚠️ Chỉ đạt {pct}% kế hoạch — chưa hoàn thành")

    if metrics["notes"]:
        lines.append("📝 Ghi chú đặc biệt:")
        for note in metrics["notes"]:
            lines.append(f"• {note}")

    if prev_total_actual is not None and ta is not None:
        diff = ta - prev_total_actual
        sign = "+" if diff >= 0 else ""
        lines.append(f"🔄 So với báo cáo trước: {sign}{diff} sản phẩm")

    return "\n".join(lines)


def export_tab_as_png(creds, sheet_id, gid, a1_range=None):
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
        f"?format=pdf&gid={gid}"
        f"&size=A4&portrait=false&scale=4&gridlines=false"
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
    if len(images) == 1:
        images[0].save(out_path, "PNG")
    else:
        # Phòng trường hợp vẫn tràn hơn 1 trang -> ghép các trang lại theo chiều dọc
        total_width = max(img.width for img in images)
        total_height = sum(img.height for img in images)
        from PIL import Image
        combined = Image.new("RGB", (total_width, total_height), "white")
        y = 0
        for img in images:
            combined.paste(img, (0, y))
            y += img.height
        combined.save(out_path, "PNG")
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


def main():
    sheet_id = os.environ["SHEET_ID"]
    creds = load_credentials()
    state = load_state()
    sent = state.get("sent", {})

    today = datetime.now(VN_TZ)
    earliest_allowed = today - timedelta(days=LOOKBACK_DAYS)

    if sent:
        last_sent_date = max(
            datetime.strptime(k, "%d%m%y").replace(tzinfo=VN_TZ) for k in sent.keys()
        )
        start_date = last_sent_date + timedelta(days=1)
        if start_date < earliest_allowed:
            start_date = earliest_allowed  # tránh gửi dồn quá nhiều nếu bot ngừng chạy lâu ngày
    else:
        start_date = earliest_allowed

    end_date = today - timedelta(days=1)  # hôm qua

    any_sent = False
    d = start_date
    while d <= end_date:
        ddmmyy = d.strftime("%d%m%y")

        base_name = f"{TAB_PREFIX} {ddmmyy}"
        bs_name = f"{base_name} BS"
        matched_name, gid = find_sheet_gid(creds, sheet_id, [bs_name, base_name])

        if gid is None:
            print(f"Chưa có tab cho ngày {ddmmyy}. Bỏ qua, không chặn các ngày sau.")
            d += timedelta(days=1)
            continue

        values = fetch_sheet_values(creds, sheet_id, matched_name)
        metrics = parse_report_metrics(values)

        if (
            not metrics
            or metrics.get("total_actual") is None
            or metrics.get("so_ma_hang", 0) == 0
        ):
            print(f"Tab \"{matched_name}\" tồn tại nhưng dữ liệu chưa đầy đủ. Bỏ qua, thử lại sau.")
            d += timedelta(days=1)
            continue

        prev_ddmmyy = (d - timedelta(days=1)).strftime("%d%m%y")
        prev_total_actual = sent.get(prev_ddmmyy)

        a1_range = get_used_range_a1(values)
        image_path = export_tab_as_png(creds, sheet_id, gid, a1_range)
        caption = build_caption(matched_name, metrics, prev_total_actual)
        send_telegram_photo(image_path, caption)

        sent[ddmmyy] = metrics["total_actual"]
        state["sent"] = sent
        save_state(state)  # lưu ngay sau mỗi lần gửi, tránh gửi trùng nếu có lỗi giữa chừng
        print(f"Đã gửi thành công tab: {matched_name}")
        any_sent = True
        d += timedelta(days=1)

    if not any_sent:
        print("Không có báo cáo mới nào cần gửi trong lần kiểm tra này.")


if __name__ == "__main__":
    main()
