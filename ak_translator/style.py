"""원문 블록의 색과 배경을 분석해서 오버레이가 원본에 묻어가게 만든다.

핵심 아이디어 두 가지.

1. **색 추출** - 블록 안 픽셀을 밝기로 두 덩어리(글자/배경)로 나눠서, 원문이 쓰던
   글자색과 배경색을 그대로 가져다 쓴다. 검은 상자를 얹는 대신 원본과 같은 톤이 된다.
2. **글자 지우기 판** - 원문 조각에서 글자 획만 지운 이미지를 만들어 배경으로 깐다.
   단색 배경이든 그라데이션이든 원래 배경이 그대로 재현되므로 이질감이 거의 없다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

# 글자 지우기를 이 정도 글자 높이로 축소해서 수행한다 (속도 + 결과 안정)
_WORK_LINE_H = 12
_MAX_KERNEL = 11
# 원문 지우기에서 글자 상자 안 바탕색과 이 이상(RGB 거리) 다르면 강조색 글자로 보고 같이 지운다
COLOR_TEXT_MIN = 48.0


@dataclass
class BlockStyle:
    bg: tuple[int, int, int]
    fg: tuple[int, int, int]
    dark_text: bool
    ink_h: int = 0  # 글자 획이 실제로 차지하는 세로 높이 (글자 크기 맞춤의 기준)
    plate: Image.Image | None = None  # 글자를 지운 배경 조각 (blend 모드용)

    @property
    def bg_hex(self) -> str:
        return "#%02X%02X%02X" % self.bg

    @property
    def fg_hex(self) -> str:
        return "#%02X%02X%02X" % self.fg


def _luma(arr: np.ndarray) -> np.ndarray:
    return (
        arr[:, :, 0] * 0.299 + arr[:, :, 1] * 0.587 + arr[:, :, 2] * 0.114
    ).astype(np.float32)


def _otsu(gray: np.ndarray) -> float:
    """두 덩어리로 가르는 밝기 경계값."""
    hist, _ = np.histogram(gray, bins=256, range=(0.0, 256.0))
    total = gray.size
    if total == 0:
        return 128.0
    levels = np.arange(256, dtype=np.float64)
    w_b = np.cumsum(hist).astype(np.float64)
    w_f = total - w_b
    sum_b = np.cumsum(levels * hist)
    sum_total = sum_b[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_b = np.where(w_b > 0, sum_b / np.maximum(w_b, 1), 0.0)
        mean_f = np.where(w_f > 0, (sum_total - sum_b) / np.maximum(w_f, 1), 0.0)
    between = w_b * w_f * (mean_b - mean_f) ** 2
    between[(w_b == 0) | (w_f == 0)] = -1.0
    return float(np.argmax(between))


def _median_rgb(arr: np.ndarray, mask: np.ndarray, fallback: tuple[int, int, int]):
    if mask.sum() < 4:
        return fallback
    sel = arr[mask]
    return tuple(int(v) for v in np.median(sel, axis=0))


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la = a[0] * 0.299 + a[1] * 0.587 + a[2] * 0.114
    lb = b[0] * 0.299 + b[1] * 0.587 + b[2] * 0.114
    return abs(la - lb)


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """마스크를 radius 만큼 부풀린다 (획 가장자리의 안티에일리어싱까지 덮으려고)."""
    if radius <= 0:
        return mask
    k = radius * 2 + 1
    img = Image.fromarray((mask * 255).astype(np.uint8))
    grown = img.filter(ImageFilter.MaxFilter(min(k, 9)))
    if k > 9:  # PIL 커널 상한을 넘으면 여러 번 돌린다
        for _ in range((k - 9) // 2):
            grown = grown.filter(ImageFilter.MaxFilter(3))
    return np.asarray(grown) > 127


def _inpaint(arr: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    """마스크 픽셀만 주변 배경으로 메운다. 나머지 픽셀은 원본 그대로 둔다.

    원본 픽셀을 건드리지 않는 것이 핵심이다. 그래야 판을 원래 자리에 얹었을 때
    가장자리에 이음매가 보이지 않는다.
    """
    try:
        import cv2  # opencv 가 있으면 훨씬 자연스럽게 메운다

        h, w = mask.shape
        bgr = np.ascontiguousarray(arr[:, :, ::-1])
        mask_u8 = mask.astype(np.uint8)

        # 넓은 대사 한 줄은 인페인팅이 100ms 를 넘긴다. 어차피 '만들어 내는' 픽셀이니
        # 절반 해상도로 메우고 되키운다. 원본 픽셀은 아래에서 그대로 되살리므로
        # 이음매는 생기지 않고, 메운 자리만 살짝 부드러워질 뿐이다.
        if h * w > 120_000:
            sw, sh = max(2, w // 2), max(2, h // 2)
            small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_AREA)
            smask = cv2.resize(mask_u8, (sw, sh), interpolation=cv2.INTER_NEAREST)
            # 축소하면서 얇아진 획이 새지 않도록 한 겹 부풀린다
            smask = cv2.dilate(smask, np.ones((3, 3), np.uint8))
            filled = cv2.inpaint(small, smask, 3, cv2.INPAINT_TELEA)
            filled = cv2.resize(filled, (w, h), interpolation=cv2.INTER_LINEAR)
        else:
            # inpaintRadius 는 '메울 때 참고할 주변 폭'이라 획 두께와 무관하다.
            # 3 을 넘기면 품질은 그대로인데 시간이 급격히 늘어난다.
            filled = cv2.inpaint(bgr, mask_u8, 3, cv2.INPAINT_TELEA)

        rgb = np.ascontiguousarray(filled[:, :, ::-1])
        return np.where(mask[:, :, None], rgb, arr)
    except Exception:
        pass

    # 대체 수단: 마스크를 배경 중앙값으로 채운 뒤 흐려서 자연스럽게 잇는다
    bg = _median_rgb(arr, ~mask, (0, 0, 0))
    filled = arr.copy()
    filled[mask] = bg
    blurred = np.asarray(
        Image.fromarray(filled).filter(ImageFilter.GaussianBlur(radius=max(1.5, radius * 1.5))),
        dtype=np.uint8,
    )
    out = arr.copy()
    out[mask] = blurred[mask]
    return out


def _erase_text(crop: Image.Image, dark_text: bool, line_h: int, inner=None) -> Image.Image:
    """글자 획만 지우고 배경은 원본 그대로 남긴 이미지.

    inner 는 crop 안의 원문 글자 상자. 주면 밝기 경계를 그 안에서만 잰다. 둘레까지 재면 옆의 진한 글자에
    경계를 빼앗긴다 (터미널의 회색 '集成战略' 은 아래 큰 검은 제목 때문에 지워지지 않았다). 지우기는 조각 전체에
    한다. 위아래로 붙은 줄은 바탕 조각이 몇 픽셀 겹치는데, 그 자리에 옆 줄 글자가 남아 있으면 얼룩이 된다.
    """
    w, h = crop.size
    if w < 4 or h < 4:
        return crop

    arr = np.asarray(crop, dtype=np.uint8)
    gray = _luma(arr.astype(np.float32))
    thr = _otsu(gray)
    if inner is not None:
        ix, iy, iw, ih = inner
        pad = 3
        y0, y1, x0, x1 = max(0, iy - pad), min(h, iy + ih + pad), max(0, ix - pad), min(w, ix + iw + pad)
        if y1 > y0 and x1 > x0:
            thr = _otsu(gray[y0:y1, x0:x1])
    mask = (gray <= thr) if dark_text else (gray > thr)
    if inner is not None and y1 > y0 and x1 > x0:
        # 강조색 글자는 밝기 경계 하나로는 안 잡힌다: 흰 글자가 많으면 경계가 올라가 청록 강조('多边贸易分队',
        # 날짜)가 그대로 남아 번역문과 겹쳤다. 글자 상자 안에서 바탕색과 확연히 다른 색은 모두 글자로 본다
        region = arr[y0:y1, x0:x1].astype(np.float32)
        rg = gray[y0:y1, x0:x1]
        bg_sel = (rg > thr) if dark_text else (rg <= thr)
        if int(bg_sel.sum()) >= 16:
            bg = np.median(region[bg_sel], axis=0)
            dist = np.sqrt(((region - bg) ** 2).sum(axis=2))
            mask[y0:y1, x0:x1] |= dist > max(COLOR_TEXT_MIN, float(np.percentile(dist, 97)) * 0.35)

    radius = max(1, int(round(max(6.0, line_h) * 0.09)))
    mask = _dilate(mask, radius)
    return Image.fromarray(_inpaint(arr, mask, radius))


@dataclass
class Plate:
    """글자를 지운 배경 조각과, 그것이 놓일 자리(캡처 영역 기준 좌표)."""

    image: Image.Image
    x: int
    y: int


def make_plate(
    image: Image.Image,
    box: tuple[int, int, int, int],
    dark_text: bool,
    line_h: int,
) -> Plate | None:
    """블록 자리의 원본에서 글자만 지운 배경 조각을 만든다.

    번역문이 원문보다 길어져 상자가 커질 것에 대비해 넉넉하게 잘라 둔다.
    오버레이는 이 조각에서 필요한 만큼만 잘라 쓰므로 확대/축소로 인한 이음매가 없다.
    """
    x, y, w, h = box
    # 번역문이 길어질 여유를 두되, 넓은 블록에서 여백이 과해지지 않게 상한을 둔다.
    # 짧은 버튼은 오버레이가 2.2배까지 넓히므로 양쪽 0.6배씩 지워 둔다
    mx = min(max(24, int(w * 0.6)), 140)
    my = max(10, int(line_h * 0.8))
    x0 = max(0, x - mx)
    y0 = max(0, y - my)
    x1 = min(image.width, x + w + mx)
    y1 = min(image.height, y + h + my)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = image.crop((x0, y0, x1, y1)).convert("RGB")
    return Plate(image=_erase_text(crop, dark_text, line_h, (x - x0, y - y0, w, h)), x=x0, y=y0)


def analyze(
    image: Image.Image,
    box: tuple[int, int, int, int],
    line_h: int,
    pad: int = 6,
    want_plate: bool = False,
) -> BlockStyle:
    """원문 이미지에서 이 블록의 글자색 / 배경색을 뽑아낸다."""
    x, y, w, h = box
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(image.width, x + w + pad)
    y1 = min(image.height, y + h + pad)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return BlockStyle(bg=(17, 19, 24), fg=(242, 244, 248), dark_text=False)

    crop = image.crop((x0, y0, x1, y1)).convert("RGB")
    arr = np.asarray(crop, dtype=np.float32)
    gray = _luma(arr)

    thr = _otsu(gray)
    dark_mask = gray <= thr
    light_mask = ~dark_mask

    # 테두리 픽셀이 많이 속한 쪽을 배경으로 본다
    border = np.zeros(gray.shape, dtype=bool)
    b = max(1, min(pad, gray.shape[0] // 4, gray.shape[1] // 4))
    border[:b, :] = border[-b:, :] = True
    border[:, :b] = border[:, -b:] = True
    dark_is_bg = int((dark_mask & border).sum()) >= int((light_mask & border).sum())

    bg_mask = dark_mask if dark_is_bg else light_mask
    fg_mask = light_mask if dark_is_bg else dark_mask
    dark_text = not dark_is_bg

    rgb = np.asarray(crop, dtype=np.uint8)
    bg = _median_rgb(rgb, bg_mask, (17, 19, 24))
    fg = _median_rgb(rgb, fg_mask, (242, 244, 248))

    # 대비가 부족하면 읽을 수 있게 흑/백으로 밀어준다
    if _contrast(bg, fg) < 60:
        bg_luma = bg[0] * 0.299 + bg[1] * 0.587 + bg[2] * 0.114
        fg = (16, 18, 22) if bg_luma > 140 else (245, 247, 250)
        dark_text = bg_luma > 140

    ink_h = _ink_height(fg_mask, pad_top=y - y0, pad_bottom=y1 - (y + h))
    plate = _erase_text(crop, dark_text, line_h) if want_plate else None
    return BlockStyle(bg=bg, fg=fg, dark_text=dark_text, ink_h=ink_h, plate=plate)


def _ink_height(fg_mask: np.ndarray, pad_top: int, pad_bottom: int) -> int:
    """글자 픽셀이 실제로 걸쳐 있는 세로 범위.

    OCR 이 주는 상자 높이는 엔진마다, 글자마다 들쭉날쭉하다. 실제 획이 차지하는
    높이를 재면 제목/본문을 구분하고 번역문 크기를 맞추는 기준이 훨씬 안정적이다.
    """
    h, w = fg_mask.shape
    # 테두리로 넣은 여백은 빼고 본다
    top = max(0, min(pad_top, h - 1))
    bottom = max(top + 1, h - max(0, pad_bottom))
    inner = fg_mask[top:bottom]
    if inner.size == 0:
        return h
    rows = inner.sum(axis=1)
    hit = np.nonzero(rows > max(1.0, w * 0.02))[0]
    if hit.size == 0:
        return int(bottom - top)
    return int(hit[-1] - hit[0] + 1)


ROOM_TOL = 90     # 바탕색과 이 이내(RGB 차이 합)면 바탕으로 본다. 카드 아래의 두 단 회색(27·51)은 한 바탕
ROOM_FREE = 0.6   # 한 세로줄에서 이 비율 이상이 바탕색이면 빈 바탕이다
ROOM_STOP = 3     # 바탕이 아닌 세로줄이 이만큼 이어지면 거기가 끝이다


def bg_room(image: Image.Image, box: tuple[int, int, int, int], bg: tuple[int, int, int],
            reach: int) -> tuple[int, int] | None:
    """글자 둘레의 바탕(카드 이름판, 버튼)이 가로로 어디까지 이어지는가 (x0, x1).

    번역문이 원문보다 길면 옆으로 넓히는데, 바탕 밖까지 나가면 이름판을 뚫고 옆 카드나 밝은
    바탕 위로 튀어나온다. 원문 줄 높이에서 바탕색이 이어지는 데까지만 쓰게 한다.
    양옆 모두 바로 바탕이 끊기면(그림 위의 글자) 알 수 없으므로 None.
    """
    x, y, w, h = box
    x0, x1 = max(0, x - reach), min(image.width, x + w + reach)
    if h < 4 or x1 - x0 < w + 4:
        return None
    band = np.asarray(image.crop((x0, y, x1, y + h)).convert("RGB"), dtype=np.int16)
    free = (np.abs(band - np.array(bg, dtype=np.int16)).sum(axis=2) < ROOM_TOL).mean(axis=0) >= ROOM_FREE

    def edge(start: int, step: int) -> int:
        """start 에서 step 방향으로 바탕이 이어지는 마지막 세로줄."""
        last, miss, i = start - step, 0, start
        while 0 <= i < len(free):
            if free[i]:
                last, miss = i, 0
            else:
                miss += 1
                if miss >= ROOM_STOP:
                    break
            i += step
        return last

    left = x0 + edge(x - x0 - 2, -1)          # 글자 가장자리의 흐린 픽셀은 건너뛰고 본다
    right = x0 + edge(x + w - x0 + 1, 1) + 1
    if (x - left) + (right - x - w) < 12:
        return None
    return left, right
