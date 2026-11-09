"""
Bot gửi Báo cáo Đóng gói Tổ May vào Telegram (cùng bot, cùng nhóm với BC Đóng Gói mút và BH).

- Đọc mọi tab "BC ĐÓNG GÓI MAY ddmmyy" (ưu tiên bản " BS") của sheet Báo cáo đóng gói May.
- Tab nào có dữ liệu (tổng thực tế > 0), ngày <= hôm nay và chưa từng gửi -> gửi ảnh + caption.
- Tab do cổng Apps Script tạo đủ dữ liệu ngay, nên không chờ sang hôm sau như bot mút.
- Đã gửi lưu ở state/may_sent.json.
"""

import csv
import json
import os
import re
import unicodedata
from datetime import datetime

SHEET_ID = "1WL76U5JEshcv49Ina6ttqATuSrkCelVFiYjs3_j40Yw"
TAB_RE = re.compile(r"^BC ĐÓNG GÓI MAY (\d{6})( BS)?$")
STATE_FILE = "state/may_sent.json"
START = datetime(2026, 11, 9)  # tab cũ hơn ngày bật bot: bỏ qua, không gửi lại


def norm(c):
    return unicodedata.normalize("NFC", " ".join(str(c).split()))


def num(s):
    return int(re.sub(r"\D", "", str(s)) or 0)


def parse(rows):
    h = next(i for i, r in enumerate(rows) if any(norm(c).endswith("Kế hoạch") for c in r))
    head = [norm(c) for c in rows[h]]

    def idx(label):
        return next(j for j, c in enumerate(head) if c == label or c.endswith(" " + label))

    col = {k: idx(k) for k in ("Mã hàng", "Tên nệm", "Kế hoạch", "Thực tế", "Ghi chú")}
    items = []
    for r in rows[h + 1:]:
        r = [str(c).strip() for c in r] + [""] * len(head)
        first = next((c for c in r if c), "")
        if first.upper() == "TỔNG":
            break
        if r[col["Tên nệm"]]:
            items.append(r)
    return col, items


def caption(tab, rows):
    col, items = parse(rows)
    kh = sum(num(r[col["Kế hoạch"]]) for r in items)
    tt = sum(num(r[col["Thực tế"]]) for r in items)
    if not tt:
        return None, 0
    pct = round(tt * 100 / kh, 1) if kh else None
    out = [f"🧵 {tab}", "", f"📈 Tổng: Kế hoạch {kh} | Thực tế {tt}" + (f" ({pct}%)" if pct is not None else ""),
           f"🏷️ Số dòng: {len(items)} dòng sản phẩm"]
    if pct is not None:
        out.append(f"✅ Hoàn thành {pct}% kế hoạch" if pct >= 100 else f"⚠️ Chỉ đạt {pct}% kế hoạch — chưa hoàn thành")
    notes = [f"{r[col['Mã hàng']]} – {r[col['Ghi chú']]}" for r in items if r[col["Ghi chú"]]]
    if notes:
        out.append("📝 Ghi chú đặc biệt:")
        out += [f"• {n}" for n in notes]
    return "\n".join(out)[:1024], tt


def main():
    from googleapiclient.discovery import build
    from report_bot import (VN_TZ, export_tab_as_png, fetch_sheet_values,
                            get_used_range_a1, load_credentials, send_telegram_photo)
    from bh_report import crop_white

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
        m = TAB_RE.match(norm(p["title"]))
        if m and (m.group(2) or m.group(1) not in tabs):
            tabs[m.group(1)] = (p["title"], p["sheetId"])
    day = lambda k: datetime.strptime(k, "%d%m%y")
    keys = sorted((k for k in tabs if START <= day(k) <= today), key=day)

    for k in keys:
        if k in sent:
            continue
        rows = fetch_sheet_values(creds, SHEET_ID, tabs[k][0])
        cap, total = caption(tabs[k][0], rows)
        if not total:
            print(f"Tab {tabs[k][0]} chưa có dữ liệu, bỏ qua.")
            continue
        img = export_tab_as_png(creds, SHEET_ID, tabs[k][1], get_used_range_a1(rows))
        crop_white(img)
        send_telegram_photo(img, cap)
        sent.add(k)
        os.makedirs("state", exist_ok=True)
        json.dump(sorted(sent, key=day), open(STATE_FILE, "w", encoding="utf-8"))  # lưu ngay, tránh gửi trùng
        print(f"Đã gửi {tabs[k][0]}")


if __name__ == "__main__":
    if os.environ.get("MAY_SELFTEST"):
        rows = list(csv.reader(open(os.environ["MAY_SELFTEST"], encoding="utf-8")))
        cap, total = caption("BC ĐÓNG GÓI MAY 091126", rows)
        assert total == 561 and "Kế hoạch 561 | Thực tế 561 (100.0%)" in cap and "18 dòng" in cap \
            and "ANSTD20-1 – ⚠ NGOÀI CỠ 88×204×20" in cap, cap
        print("selftest ok\n" + cap)
    else:
        main()
