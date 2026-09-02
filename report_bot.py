"""
Bot gửi báo cáo Google Sheet hàng ngày vào Telegram dưới dạng ảnh + số liệu tổng hợp.

Cách hoạt động:
1. Báo cáo của ngày X được nhập vào sheet vào ngày X+1 -> bot luôn tìm tab của HÔM QUA
2. Ưu tiên tìm tab có hậu tố " BS" (bổ sung) trước, nếu không có thì dùng tab gốc
3. Đọc dữ liệu bảng qua link công khai của sheet (không cần service account), tính:
   tổng kế hoạch/thực tế/%, số mã hàng, ghi chú đặc biệt, so sánh với báo cáo lần trước
   (lưu trong state/last_sent.txt)
4. Tự vẽ ảnh bảng báo cáo bằng PIL (không cần Google export + poppler) -> gửi Telegram kèm caption

Các biến môi trường cần thiết:
- TELEGRAM_BOT_TOKEN : token của bot Telegram
- TELEGRAM_CHAT_ID   : chat id của group (số âm)
- SHEET_ID           : ID của Google Sheet (lấy từ URL sheet) — sheet phải ở chế độ
                        chia sẻ "Anyone with the link can view".
"""

import os
import sys
import csv
import io
import json
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
from PIL import Image, ImageDraw, ImageFont

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------- Cấu hình ----------
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


def fetch_sheet_csv(sheet_id, sheet_name):
    """Đọc 1 tab qua link công khai (sheet phải share 'Anyone with the link: Viewer')."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return list(csv.reader(io.StringIO(resp.text)))


def extract_report_date_ddmmyy(rows):
    """Google trả CSV của tab đầu tiên nếu tên tab không tồn tại, nên phải tự đối chiếu
    ngày in trong nội dung báo cáo (dòng 'BÁO CÁO SẢN XUẤT NGÀY dd/mm/yyyy') để xác nhận
    đúng tab, thay vì tin tưởng mù tên tab đã yêu cầu."""
    text = " ".join(str(c) for row in rows[:3] for c in row)
    m = re.search(r"NG[ÀA]Y\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{int(d):02d}{int(mo):02d}{y[2:]}"


def find_matching_tab(sheet_id, candidate_names, target_ddmmyy):
    for name in candidate_names:
        rows = fetch_sheet_csv(sheet_id, name)
        if extract_report_date_ddmmyy(rows) == target_ddmmyy:
            return name, rows
    return None, None


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
    """Đọc bảng báo cáo, trả về dict số liệu tổng hợp (hoặc None nếu không nhận diện được).

    Một số ô tiêu đề trong sheet bị dính chữ letterhead công ty ở đầu (VD: "...www.nemthanhcong.com Tên nệm"
    thay vì chỉ "Tên nệm" gọn), nên phải so khớp theo hậu tố (endswith) chứ không so khớp tuyệt đối."""
    KNOWN_LABELS = ["Mã hàng", "Tên nệm", "Kích thước", "Kế hoạch", "Thực tế", "Ghi chú"]
    header_idx = None
    col_idx = {}
    for i, row in enumerate(values):
        cells = [str(c).strip() for c in row]
        if any(c == "Kế hoạch" for c in cells) and any(c == "Thực tế" for c in cells):
            header_idx = i
            for label in KNOWN_LABELS:
                idx = next((j for j, c in enumerate(cells) if c == label or c.endswith(" " + label)), None)
                if idx is not None:
                    col_idx[label] = idx
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

    missing_ma_hang = sum(
        1 for r in product_rows
        if get_cell(r, "Tên nệm") != "" and get_cell(r, "Mã hàng") == ""
    )

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
        "missing_ma_hang": missing_ma_hang,
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


def _trim_used_range(values):
    last_row = 0
    last_col = 0
    for i, row in enumerate(values):
        for j, cell in enumerate(row):
            if str(cell).strip() != "":
                last_row = max(last_row, i + 1)
                last_col = max(last_col, j + 1)
    return [row[:last_col] for row in values[:last_row]]


def render_table_image(values, out_path):
    """Tự vẽ ảnh bảng báo cáo bằng PIL, thay cho việc xuất ảnh qua Google + poppler."""
    rows = _trim_used_range(values)
    if not rows:
        raise RuntimeError("Không có dữ liệu để vẽ ảnh")

    max_len = 28

    def cell_text(c):
        s = str(c).strip()
        return (s[: max_len - 1] + "…") if len(s) > max_len else s

    rows = [[cell_text(c) for c in row] for row in rows]
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    try:
        font = ImageFont.truetype("arial.ttf", 18)
        font_bold = ImageFont.truetype("arialbd.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
        font_bold = font

    pad_x, pad_y = 10, 6
    tmp = Image.new("RGB", (10, 10))
    measure = ImageDraw.Draw(tmp)
    col_widths = [0] * ncols
    row_height = 0
    for r in rows:
        for j, c in enumerate(r):
            bbox = measure.textbbox((0, 0), c or " ", font=font_bold)
            col_widths[j] = max(col_widths[j], (bbox[2] - bbox[0]) + pad_x * 2)
            row_height = max(row_height, (bbox[3] - bbox[1]) + pad_y * 2)

    total_width = sum(col_widths)
    total_height = row_height * len(rows)
    img = Image.new("RGB", (total_width + 1, total_height + 1), "white")
    draw = ImageDraw.Draw(img)

    header_idx = next((i for i, r in enumerate(rows) if "Kế hoạch" in r and "Thực tế" in r), None)
    total_idx = next((i for i, r in enumerate(rows) if r and r[0].strip().upper() == "TỔNG"), None)

    y = 0
    for i, r in enumerate(rows):
        x = 0
        is_bold = i in (header_idx, total_idx)
        for j, c in enumerate(r):
            w = col_widths[j]
            if is_bold:
                draw.rectangle([x, y, x + w, y + row_height], fill="#e8e8e8")
            draw.rectangle([x, y, x + w, y + row_height], outline="#999999")
            draw.text((x + pad_x, y + pad_y), c, fill="black", font=font_bold if is_bold else font)
            x += w
        y += row_height

    img.save(out_path, "PNG")
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
        matched_name, values = find_matching_tab(sheet_id, [bs_name, base_name], ddmmyy)

        if matched_name is None:
            print(f"Chưa có tab cho ngày {ddmmyy}. Bỏ qua, không chặn các ngày sau.")
            d += timedelta(days=1)
            continue

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

        out_path = os.path.join(tempfile.gettempdir(), "mut_bc_dong_goi_report.png")
        image_path = render_table_image(values, out_path)
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
