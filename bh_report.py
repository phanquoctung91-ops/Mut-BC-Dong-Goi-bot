"""
Bot gửi Báo cáo Bảo hành Mút vào Telegram (cùng bot, cùng nhóm với BC Đóng Gói).

- Đọc mọi tab "BC BẢO HÀNH MÚT ddmmyy" (ưu tiên bản " BS") của sheet bảo hành.
- Tab nào có dữ liệu (tổng > 0), ngày <= hôm nay và chưa từng gửi -> gửi ảnh + caption.
- Đã gửi lưu ở state/bh_sent.json, tab không theo ngày cố định nên không cần quét bù.
"""

import csv
import json
import os
import re
from collections import Counter
from datetime import datetime

from PIL import Image, ImageOps

from report_bot import (VN_TZ, export_tab_as_png, fetch_sheet_values,
                        get_used_range_a1, load_credentials, send_telegram_photo)
from googleapiclient.discovery import build

SHEET_ID = "177wqQjNq8B5n3dG-Fe1N43atitmX66J5phaWYsLyJq0"
TAB_RE = re.compile(r"^BC BẢO HÀNH MÚT (\d{6})( BS)?$")
STATE_FILE = "state/bh_sent.json"
WORK_DAYS = 26  # số ngày làm việc/tháng dùng tính trung bình


def parse(rows):
    h = next(i for i, r in enumerate(rows) if "Số Lượng" in [str(c).strip() for c in r])
    head = [str(c).strip() for c in rows[h]]
    col = {c: j for j, c in enumerate(head) if c}
    errs = head[col["Số Lượng"] + 1: col["Nguyên nhân"]]
    items = []
    for r in rows[h + 1:]:
        r = [str(c).strip() for c in r] + [""] * len(head)
        if "TỔNG" in r[:4] or not r[col["Tên Nệm"]]:
            continue
        items.append(r)
    return col, errs, items


def caption(tab, rows, month_total=None):
    col, errs, items = parse(rows)
    q = lambda r: int(r[col["Số Lượng"]] or 1)
    total = sum(q(r) for r in items)
    if not total:
        return None, 0
    loi, ng, xl, to = Counter(), Counter(), Counter(), Counter()
    for r in items:
        for e in errs:
            if r[col[e]].upper() == "X":
                loi[e] += q(r)
        ng[(r[col["Nguyên nhân"]] or r[col.get("Lỗi khác", 0)] or "Không ghi").rstrip(".")] += q(r)
        xl["Hủy" if r[col["Hướng xử lý"]].lower().startswith("hủy") else "Sửa"] += q(r)
        to["Tổ " + (r[col["Tổ Phụ Trách"]] or "?")] += q(r)
    pct = lambda v: f"{v}/{total} ({round(v * 100 / total)}%)"
    sec = lambda title, c: [title] + [f"• {k}: {pct(v)}" for k, v in c.most_common()]
    out = [f"🛠️ {tab}", "", f"📦 Tổng: {total} nệm (Sửa {xl['Sửa']}, Hủy {xl['Hủy']})", ""]
    out += sec("⚠️ Lỗi:", loi) + [""]
    out += sec("🔎 Nguyên nhân:", ng) + [""]
    out.append("👥 Tổ thực hiện: " + ", ".join(f"{k}: {v}" for k, v in sorted(to.items())))
    if month_total is not None:
        avg = f"{month_total / WORK_DAYS:.1f}".replace(".", ",")
        out.append(f"📅 TB tháng {int(tab[-4:-2])}: {month_total} nệm / {WORK_DAYS} ngày = {avg} nệm/ngày")
    return "\n".join(out)[:1024], total


def crop_white(path):
    im = Image.open(path).convert("RGB")
    b = ImageOps.invert(im).getbbox()
    if b:
        im.crop((max(b[0] - 20, 0), max(b[1] - 10, 0), b[2] + 20, b[3] + 10)).save(path)


def main():
    try:
        sent = set(json.load(open(STATE_FILE, encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        sent = set()
    today = datetime.now(VN_TZ).replace(tzinfo=None)
    creds = load_credentials()
    meta = build("sheets", "v4", credentials=creds).spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="sheets(properties(sheetId,title))").execute()

    tabs = {}  # ddmmyy -> (title, gid); bản BS thắng bản gốc
    for s in meta["sheets"]:
        p = s["properties"]
        m = TAB_RE.match(p["title"].strip())
        if m and (m.group(2) or m.group(1) not in tabs):
            tabs[m.group(1)] = (p["title"], p["sheetId"])
    day = lambda k: datetime.strptime(k, "%d%m%y")
    keys = sorted((k for k in tabs if day(k) <= today), key=day)

    cache = {}
    def rows_total(k):
        if k not in cache:
            rows = fetch_sheet_values(creds, SHEET_ID, tabs[k][0])
            cache[k] = (rows, caption(tabs[k][0], rows)[1])
        return cache[k]

    for k in keys:
        if k in sent:
            continue
        rows, total = rows_total(k)
        if not total:
            print(f"Tab {tabs[k][0]} chưa có dữ liệu, bỏ qua.")
            continue
        month = sum(rows_total(j)[1] for j in keys if day(j) <= day(k) and k[2:] == j[2:])
        cap, _ = caption(tabs[k][0], rows, month)
        img = export_tab_as_png(creds, SHEET_ID, tabs[k][1], get_used_range_a1(rows))
        crop_white(img)
        send_telegram_photo(img, cap)
        sent.add(k)
        os.makedirs("state", exist_ok=True)
        json.dump(sorted(sent, key=day), open(STATE_FILE, "w", encoding="utf-8"))  # lưu ngay, tránh gửi trùng
        print(f"Đã gửi {tabs[k][0]}")


if __name__ == "__main__":
    if os.environ.get("BH_SELFTEST"):
        rows = list(csv.reader(open(os.environ["BH_SELFTEST"], encoding="utf-8")))
        cap, total = caption("BC BẢO HÀNH MÚT 221026", rows, 76)
        assert total == 27 and "• Bẩn áo: 21/27 (78%)" in cap and "Tổ 3: 18" in cap and "= 2,9 nệm/ngày" in cap, cap
        print("selftest ok")
    else:
        main()
