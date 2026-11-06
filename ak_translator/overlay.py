"""게임 화면 위에 번역문을 덮어 그리는 투명 창.

블록을 하나하나 따로 그린다. 스토리에서 대사만 바뀌면 그 블록만 지우고 새로 그리므로
화면 전체가 깜빡이지 않는다. 창은 마우스를 통과시키고 화면 캡처에서도 빠진다.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

import numpy as np
from PIL import Image, ImageTk

from . import config, winutil

# 이 색으로 칠해진 픽셀은 완전히 투명해지고 마우스도 통과한다.
# 글자 가장자리가 이 색과 섞이므로 검정에 가까운 색을 써서 그림자처럼 보이게 한다.
CHROMA = "#010203"
CHROMA_RGB = (1, 2, 3)
CHROMA_SAFE = (2, 3, 4)
OUTLINE_STROKE = "#0A0A0C"
PAD = 6               # 블록 상자를 원문 둘레로 넉넉히 덮는 여백
MIN_TEXT_WIDTH = 240  # 짧은 원문이라도 이 폭까지는 옆으로 늘려 쓴다
WIDEN = 2.2           # 한 줄짜리는 원문 폭의 이 배까지 옆으로 넓힌다
CENTER_MAX = 12       # 원문 폭이 줄 높이의 이 배 이하인 한 줄은 버튼·제목으로 보고 가운데에 맞춘다
NEIGHBOR_GAP = 3      # 넓힌 상자가 옆 블록 상자와 맞닿지 않게 남기는 틈 (양쪽에서 3씩)
TEXT_TAG = "text"     # 번역문 글자 (바탕 조각보다 늘 위에 둔다)
RECRUIT_TAG = "recruit"   # 공개모집 태그 테두리·조합 목록 (번역문보다 위)
PANEL_BG, PANEL_EDGE = "#15171B", "#4A4F57"
ICON = "▲"            # 번역문에서 화폐 아이콘 자리 (resolver.ICON). 항목에 'icon' 그림이 있으면 그 자리에 붙인다
ICON_FILL = 0.9       # 아이콘 높이 = 글자 줄 높이 x 이것


class Overlay:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.region: tuple[int, int, int, int] | None = None
        self._font_cache: dict[tuple, tkfont.Font] = {}
        self._drawn: dict[str, object] = {}   # 블록 id -> 그 블록의 PhotoImage (없으면 None)
        self._texts: dict[str, str] = {}      # 블록 id -> 그려 둔 번역문
        self._spans: dict[str, tuple] = {}    # 블록 id -> 그릴 때 쓸 수 있었던 가로 범위
        self._icons: dict[str, object] = {}   # 블록 id -> 번역문에 붙인 화폐 아이콘 PhotoImage
        self.hidden = False

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=CHROMA)
        self.win.attributes("-transparentcolor", CHROMA)

        self.canvas = tk.Canvas(self.win, bg=CHROMA, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)

        self.win.update_idletasks()
        self.hwnd = winutil.get_hwnd(self.win)
        winutil.set_tool_window(self.hwnd)
        winutil.set_click_through(self.hwnd, True)
        # 이게 있어야 번역문을 다시 읽지 않는다. 눈에는 보이지만 캡처에는 찍히지 않는다.
        self.capture_excluded = winutil.exclude_from_capture(self.hwnd, True)

    # ------------------------------------------------------------------ 표시
    def set_hidden(self, hidden: bool) -> None:
        """그린 내용은 둔 채로 창만 감추거나 드러낸다."""
        self.hidden = hidden
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        if self.hidden or not self._drawn:
            self.win.withdraw()
        else:
            self.win.deiconify()
            self.win.attributes("-topmost", True)

    def restyle(self) -> None:
        """표시 방식이나 글자 크기를 바꿨을 때 전부 다시 그리게 한다."""
        self._font_cache.clear()
        self.clear()

    def clear(self) -> None:
        self.canvas.delete("all")
        self._drawn.clear()
        self._texts.clear()
        self._spans.clear()
        self._icons.clear()
        self.win.withdraw()

    def show(self, region: tuple[int, int, int, int], items: dict[str, dict]) -> None:
        """items 를 화면에 맞춘다. 이미 그려진 블록은 건드리지 않는다."""
        if region != self.region:
            self.clear()
            self.region = region
            x, y, w, h = region
            self.win.geometry(f"{w}x{h}+{x}+{y}")

        # 번역문이 바뀌었거나(기계 번역 → Gemini), 원문이 옮겨 갔거나(화면이 마우스를 따라 기울 때),
        # 옆 블록이 생기거나 없어져 쓸 수 있는 폭이 달라졌으면 다시 그린다
        spans = {i: (self._span(i, item, items), tuple(item["box"])) for i, item in items.items()}
        for item_id in [i for i in self._drawn
                        if i not in items or self._texts.get(i) != items[i].get("text")
                        or self._spans.get(i) != spans[i]]:
            self.canvas.delete(_tag(item_id))
            del self._drawn[item_id]
            self._texts.pop(item_id, None)
            self._spans.pop(item_id, None)
            self._icons.pop(item_id, None)
        for item_id, item in items.items():
            if item_id not in self._drawn:
                self._draw(item_id, item, spans[item_id][0])
                self._spans[item_id] = spans[item_id]
        # 글자는 모든 바탕 조각 위에 둔다. 위아래로 붙은 줄은 바탕이 몇 픽셀 겹쳐서, 나중에 그린 아래 줄의
        # 바탕이 윗줄 글자 아랫부분을 가렸다 ('세부분류' 가 '세부부류' 로 보였다)
        self.canvas.tag_raise(TEXT_TAG)
        self.canvas.tag_raise(RECRUIT_TAG)      # 공개모집 테두리·목록은 번역문 바탕보다 위에
        self._apply_visibility()

    def _span(self, item_id: str, item: dict, items: dict) -> tuple[int, int]:
        """번역문 상자가 차지해도 되는 가로 범위 (x0, x1).

        한 줄짜리는 옆으로 넓히는데, 같은 높이에 다른 블록이 있으면 그 사이의 가운데까지만 간다.
        그러지 않으면 나란한 버튼('清除缓存' '网络检测')의 번역문이 서로 덮는다.
        """
        ow = self.region[2]
        bx, by, bw, bh = item["box"]
        reach = PAD
        if int(item.get("rows", 1)) <= 1:
            reach += max(int(bw * WIDEN), MIN_TEXT_WIDTH) - bw
        left, right = max(0, bx - reach), min(ow, bx + bw + reach)
        for other_id, other in items.items():
            if other.get("kind"):
                continue    # 공개모집 테두리·목록 상자는 번역문 자리를 좁히지 않는다
            ox, oy, obw, obh = other["box"]
            if other_id == item_id or oy >= by + bh or oy + obh <= by:
                continue    # 세로로 안 겹치면 부딪히지 않는다
            if ox >= bx + bw:
                right = min(right, (bx + bw + ox) // 2 - NEIGHBOR_GAP)
            elif ox + obw <= bx:
                left = max(left, (ox + obw + bx) // 2 + NEIGHBOR_GAP)
        room = item.get("room")
        if room:
            # 이름판·버튼 같은 바탕 밖으로는 넓히지 않는다. 자리가 모자라면 글자를 줄인다
            left, right = max(left, room[0]), min(right, room[1])
        # 원문은 어떻게든 다 덮는다
        return min(left, max(0, bx - PAD)), max(right, min(ow, bx + bw + PAD))

    # ------------------------------------------------------------------ 그리기
    def _draw(self, item_id: str, item: dict, span: tuple[int, int]) -> None:
        text = (item.get("text") or "").strip()
        tag = _tag(item_id)
        self._drawn[item_id] = None
        self._texts[item_id] = item.get("text")
        if not text:
            return
        if item.get("kind") == "mark":
            self._draw_mark(tag, item)
            return
        if item.get("kind") == "panel":
            self._draw_panel(tag, item)
            return
        oh = self.region[3]
        bx, by, bw, bh = item["box"]
        rows = max(1, int(item.get("rows", 1)))
        line_h = int(item.get("line_h") or max(8, bh // rows))
        ink_h = int(item.get("ink_h") or 0)
        style = self.cfg.get("box_style", "blend")
        bg = item.get("bg") or "#111318"
        fg = item.get("fg") or "#F2F4F8"

        # 한 줄짜리는 한글이 길어져도 작아지지 않게 옆 빈자리로 넓힌다. 버튼·제목은 원문 가운데에
        # 맞춰 양옆으로, 긴 줄과 대사는 왼쪽 끝을 지키고 오른쪽으로만. 자리가 모자라면 글자를 줄인다
        single = rows == 1
        center = single and item.get("align") != "left" and bw <= line_h * CENTER_MAX
        left, right = span
        if not center:
            left = max(0, bx - PAD)
        avail_w = max(20, right - left - PAD * 2)
        if not single:
            avail_w = min(avail_w, bw)
        font, wrapped, tw, th = self._fit(text, avail_w, bh, line_h, ink_h)

        box_w = max(bw, tw) + PAD * 2
        box_h = max(bh, th) + PAD * 2
        x0 = bx + bw // 2 - box_w // 2 if center else bx - PAD
        x0 = max(0, left, min(x0, right - box_w))
        # 글자가 원문보다 높으면 위아래로 반씩 넘친다. 아래로만 넘치면 다음 줄을 덮는다
        y0 = by - PAD - max(0, th - bh) // 2
        y0 = max(0, min(y0, oh - box_h))
        tx = x0 + PAD
        ty = y0 + PAD + (max(0, bh - th) // 2 if single else 0)

        if style == "blend":
            plate = item.get("plate")
            if plate is not None:
                self._draw_plate(tag, item_id, plate, x0, y0, box_w, box_h, bg)
            else:
                self.canvas.create_rectangle(
                    x0, y0, x0 + box_w, y0 + box_h, fill=bg, outline=bg, tags=tag
                )
        elif style == "fill":
            self.canvas.create_rectangle(
                x0, y0, x0 + box_w, y0 + box_h, fill=bg, outline=bg, tags=tag
            )
        else:
            # 자막 방식: 배경 없이 어두운 테두리를 두른다. 원문은 비쳐 보인다.
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (1, -1), (-1, 1)):
                self._put_text(tx + dx, ty + dy, wrapped, font, OUTLINE_STROKE, (tag, TEXT_TAG), None)
            fg = _light_enough(fg)
        icon = item.get("icon") if ICON in wrapped else None
        self._put_text(tx, ty, wrapped, font, fg, (tag, TEXT_TAG), icon, item_id)

    def _put_text(self, tx, ty, text, font, fill, tags, icon, item_id: str = "") -> None:
        """번역문을 쓴다. 화폐 아이콘 자리('▲')가 있고 화면에서 잘라 둔 아이콘이 있으면 그 자리에 그림을 붙인다.
        글자 맞추기(_fit)는 '▲' 폭으로 쟀으니, 그 폭 칸 가운데에 아이콘을 놓는다."""
        if ICON not in text or icon is None and item_id:
            self.canvas.create_text(tx, ty, text=text, font=font, fill=fill, anchor="nw", justify="left", tags=tags)
            return
        step = font.metrics("linespace")
        cell = font.measure(ICON)
        photo = None
        if icon is not None:
            h = max(4, int(step * ICON_FILL))
            w = max(4, min(int(cell * 1.6), round(icon.width * h / max(1, icon.height))))
            photo = ImageTk.PhotoImage(_avoid_chroma(icon.resize((w, h), Image.LANCZOS)), master=self.win)
            self._icons[item_id] = photo    # GC 방지
        for row, line in enumerate(text.split("\n")):
            x, y = tx, ty + row * step
            parts = line.split(ICON)
            for i, part in enumerate(parts):
                if part:
                    self.canvas.create_text(x, y, text=part, font=font, fill=fill, anchor="nw", tags=tags)
                    x += font.measure(part)
                if i == len(parts) - 1:
                    break
                if photo is not None:
                    self.canvas.create_image(x, y + step // 2, image=photo, anchor="w", tags=tags)
                    x += max(cell, photo.width()) + max(1, step // 10)
                    continue
                if item_id:     # 테두리 그림자(item_id 없음)는 아이콘 자리를 비워 둔다
                    self.canvas.create_text(x, y, text=ICON, font=font, fill=fill, anchor="nw", tags=tags)
                x += cell

    def _draw_mark(self, tag: str, item: dict) -> None:
        """공개모집 확정 조합에 든 태그 칸 테두리 (별 색). 상자는 칸 크기다 (recruit.Recruiter)."""
        bx, by, bw, bh = item["box"]
        width = max(3, bh // 14)
        self.canvas.create_rectangle(bx, by, bx + bw, by + bh, outline=item["color"], width=width,
                                     tags=(tag, RECRUIT_TAG))

    def _draw_panel(self, tag: str, item: dict) -> None:
        """공개모집 확정 조합 목록. 태그 칸들 아래에 두고, 아래 자리가 모자라면 위에 둔다."""
        bx, by, bw, bh = item["box"]
        font = self._font(max(11, int(item.get("line_h", 30) * 0.5)))
        step = font.metrics("linespace") + 4
        lines = item["lines"]
        pad = step // 2
        width = max(font.measure(text) for text, _color in lines) + pad * 2
        height = step * len(lines) + pad * 2
        ow, oh = self.region[2], self.region[3]
        gap = max(8, int(item.get("line_h", 30)) // 2)
        y = by + bh + gap if by + bh + gap + height <= oh else max(0, by - gap - height)
        x = max(0, min(bx, ow - width))
        self.canvas.create_rectangle(x, y, x + width, y + height, fill=PANEL_BG, outline=PANEL_EDGE, width=1,
                                     tags=(tag, RECRUIT_TAG))
        for i, (text, color) in enumerate(lines):
            self.canvas.create_text(x + pad, y + pad + i * step, text=text, font=font, fill=color, anchor="nw",
                                    tags=(tag, RECRUIT_TAG))

    def _draw_plate(self, tag, item_id, plate, x, y, w, h, bg) -> None:
        """글자를 지운 원본 배경 조각에서 상자에 해당하는 부분만 잘라 깐다."""
        w, h = max(1, w), max(1, h)
        need_x, need_y = x - plate.x, y - plate.y
        pw, ph = plate.image.size
        sx0, sy0 = max(0, need_x), max(0, need_y)
        sx1, sy1 = min(pw, need_x + w), min(ph, need_y + h)
        if sx0 == need_x and sy0 == need_y and sx1 == need_x + w and sy1 == need_y + h:
            piece = plate.image.crop((need_x, need_y, need_x + w, need_y + h))
        elif sx1 > sx0 and sy1 > sy0:
            # 상자가 판 밖으로 삐져나간 부분은 판 가장자리를 늘여 메운다.
            # 단색으로 메우면 글자 양끝에 네모난 조각이 보인다
            inner = np.asarray(plate.image.convert("RGB"))[sy0:sy1, sx0:sx1]
            piece = Image.fromarray(np.pad(
                inner, ((sy0 - need_y, need_y + h - sy1), (sx0 - need_x, need_x + w - sx1), (0, 0)),
                mode="edge"))
        else:
            piece = Image.new("RGB", (w, h), bg)
        photo = ImageTk.PhotoImage(_avoid_chroma(piece), master=self.win)
        self._drawn[item_id] = photo   # GC 방지
        self.canvas.create_image(x, y, image=photo, anchor="nw", tags=tag)

    # ------------------------------------------------------------- 글자 맞추기
    def _font(self, size: int) -> tkfont.Font:
        family = self.cfg.get("font_family") or config.FONT_FAMILY
        key = (family, size)
        font = self._font_cache.get(key)
        if font is None:
            font = tkfont.Font(root=self.root, family=family, size=size)
            self._font_cache[key] = font
        return font

    def _fit(self, text: str, max_w: int, box_h: int, line_h: int, ink_h: int = 0):
        """원문 글자 높이에 맞춘 크기에서 시작해, 상자에 들어갈 때까지 줄인다."""
        scale = float(self.cfg.get("font_scale", 1.0))
        hi = int(self.cfg.get("max_font_size", 44))
        lo = int(self.cfg.get("min_font_size", 11))
        if ink_h >= 6:
            # 원문 획 높이를 그대로 재현한다. 한글 글꼴의 획 높이 ≈ 1.28 x 포인트.
            start = int(round(ink_h / 1.28 * scale))
        else:
            start = int(round(max(6, line_h) * 0.75 * scale))
        start = max(lo, min(hi, start))

        best = None
        size = start
        while size >= lo:
            font = self._font(size)
            lines = _wrap(text, font, max_w)
            spacing = font.metrics("linespace")
            th = len(lines) * spacing
            tw = max((font.measure(l) for l in lines), default=0)
            best = (font, "\n".join(lines), tw, th)
            if th <= box_h + spacing * 0.3 or size == lo:
                break
            size -= 1
        return best


def _tag(item_id: str) -> str:
    return "b" + "".join(f"{ord(c):x}" for c in item_id)[:120]


def _avoid_chroma(img: Image.Image) -> Image.Image:
    """배경판에 크로마와 똑같은 픽셀이 있으면 구멍이 뚫리므로 1 만큼 밀어준다."""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    hit = (arr[:, :, 0] == CHROMA_RGB[0]) & (arr[:, :, 1] == CHROMA_RGB[1]) & (
        arr[:, :, 2] == CHROMA_RGB[2]
    )
    if not hit.any():
        return img
    arr = arr.copy()
    arr[hit] = CHROMA_SAFE
    return Image.fromarray(arr)


def _light_enough(hex_color: str) -> str:
    """자막 방식에서 어두운 글자색이면 흰색으로 바꿔 테두리와 대비를 만든다."""
    try:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    except (ValueError, IndexError):
        return "#FFFFFF"
    return hex_color if (r * 0.299 + g * 0.587 + b * 0.114) > 120 else "#FFFFFF"


def _wrap(text: str, font: tkfont.Font, max_w: int) -> list[str]:
    """공백 우선, 필요하면 글자 단위로 줄바꿈."""
    max_w = max(20, int(max_w))
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            continue
        cur = ""
        for token in _tokens(para):
            trial = cur + token
            if cur and font.measure(trial.rstrip()) > max_w:
                lines.append(cur.rstrip())
                cur = token.lstrip()
            else:
                cur = trial
            while font.measure(cur) > max_w and len(cur) > 1:
                cut = len(cur)
                while cut > 1 and font.measure(cur[:cut]) > max_w:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
        if cur:
            lines.append(cur.rstrip())
    return lines or [""]


def _tokens(text: str) -> list[str]:
    """공백은 앞 토큰에 붙여서 반환. 한글은 낱말째로 묶어 줄바꿈이 어절 사이에서 나게 한다."""
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch == " ":
            out.append(buf + " ")
            buf = ""
        elif "　" <= ch <= "鿿" or "＀" <= ch <= "￯":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out
