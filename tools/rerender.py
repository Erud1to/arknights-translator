"""저장한 문제 화면(debug/*/1_원본.png)을 지금 코드로 다시 읽고 오버레이를 그려 원본 위에 합성한다.

  python -I tools/rerender.py 20261104-013114 20261105-214312
  python -I tools/rerender.py --ocr windows --out build/rerender 20261104

오버레이 창은 모니터 밖(x=-6000)에 띄우고 PrintWindow 로 그 창만 찍는다 (다른 창은 찍지 않는다).
기계 번역은 부르지 않는다. 번역이 없는 블록은 비슷한 길이의 대역 글로 채워 글자 크기·줄바꿈만 본다.
결과: build/rerender/<캡처>_<ocr>.png. AK_DATA 로 다른 데이터 폴더를 쓸 수 있다.
"""
import argparse
import ctypes
import glob
import os
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import winutil  # noqa: E402

FILLER = "테라 동부의 강대국으로 넓은 영토와 많은 이동 도시를 가지고 있으며 오랜 역사 동안 번영을 누렸다 "


def print_window(hwnd: int, w: int, h: int):
    import numpy as np

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    screen = user32.GetDC(0)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    gdi32.SelectObject(dc, bmp)
    user32.PrintWindow(wintypes.HWND(hwnd), dc, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(0, screen)
    return np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)[:, :, 2::-1]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="문제 화면 다시 그리기")
    ap.add_argument("captures", nargs="+", help="debug 아래 캡처 이름(앞부분만 줘도 된다)")
    ap.add_argument("--ocr", choices=("paddle", "windows"), default="paddle")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "rerender"))
    args = ap.parse_args()

    winutil.enable_dpi_awareness()
    import tkinter as tk

    import numpy as np
    from PIL import Image

    from ak_translator import config, ppocr, recruit, style, textnorm as tn
    from ak_translator.engine import ICON, ROOM_REACH, _cover_missed, _picker, _trim_box, frame_blocks, icon_crop
    from ak_translator.ocr import WindowsOCR
    from ak_translator.overlay import Overlay
    from ak_translator.resolver import Resolver

    winutil.load_private_fonts(config.FONT_DIR)
    cfg = config.load()
    res = Resolver(cfg, os.environ.get("AK_DATA") or config.DATA_DIR)
    res.enqueue = lambda *a, **k: None      # 번역기로 보내지 않는다
    for _ in range(200):                    # 이름 사전은 뒤에서 만든다
        if res.names.entries:
            break
        time.sleep(0.05)
    meta = res.db.meta
    ocr = (ppocr.PaddleOCR(config.MODEL_DIR, meta.get("charset", ""), rare=meta.get("rare", ""))
           if args.ocr == "paddle" else WindowsOCR())
    print("OCR", ocr.name)
    rec = recruit.Recruiter(res.db, lambda zh: (res.db.text_ko(zh)[1] or zh))
    folders = sorted({os.path.dirname(p) for c in args.captures
                      for p in glob.glob(os.path.join(ROOT, "debug", c + "*", "1_원본.png"))})
    if not folders:
        raise SystemExit("그런 캡처가 없다: " + ", ".join(args.captures))
    os.makedirs(args.out, exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    ov = Overlay(root, cfg)
    winutil.exclude_from_capture(ov.hwnd, False)
    try:
        for folder in folders:
            frame = Image.open(os.path.join(folder, "1_원본.png")).convert("RGB")
            items, shown = {}, []
            print("==", os.path.basename(folder))
            for b in frame_blocks(ocr, frame, res, rec):
                r = res.resolve(b.text, allow_mt=False, masked=b.masked, pick=_picker(frame, b))
                text, via = r.text, r.via
                if text and r.trim != (0, 0):
                    b = _trim_box(b, r.trim, frame)
                if text and r.missed != (0, 0):
                    b = _cover_missed(b, r.missed)
                if not text and via == "pending":
                    n = int(tn.cjk_count(b.text) * 1.25)
                    text, via = (FILLER * (n // len(FILLER) + 1))[:n], "대역"
                print(f"  {via:8} rows={b.rows} box={b.box} {b.text[:40]}")
                if not text:
                    continue
                st = b.style
                item = {
                    "text": text, "box": b.box, "rows": b.rows, "line_h": b.line_h, "ink_h": b.ink_h,
                    "bg": st.bg_hex if st else None, "fg": st.fg_hex if st else None,
                    "plate": style.make_plate(frame, b.box, st.dark_text, b.line_h) if st else None,
                    "align": "left" if r.story else "",
                    "icon": icon_crop(frame, b) if ICON in text else None,
                    "room": (style.bg_room(frame, b.box, st.bg, max(ROOM_REACH, b.box[2] * 2))
                             if st and b.rows == 1 else None),
                }
                items[f"{tn.key(b.text)}@{b.box[0] // 16},{b.box[1] // 16}"] = item
                shown.append((b.text, item))
            items.update(rec.items([(src, it["box"], it["room"]) for src, it in shown]))
            ov.clear()
            ov.show((-6000, 0, frame.width, frame.height), items)
            for _ in range(5):
                root.update()
                time.sleep(0.05)
            arr = print_window(ov.hwnd, frame.width, frame.height)
            base = np.asarray(frame).copy()
            mask = arr.any(axis=2)          # 투명색 자리는 PrintWindow 에서 (0,0,0) 으로 나온다
            base[mask] = arr[mask]
            path = os.path.join(args.out, f"{os.path.basename(folder)}_{args.ocr}.png")
            Image.fromarray(base).save(path)
            print("  →", path)
    finally:
        res.stop()
        root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
