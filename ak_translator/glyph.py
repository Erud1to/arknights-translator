"""화면의 글자 한 자가 후보 한자 중 어느 것과 가장 닮았는지 본다.

OCR 이 반쪽으로 갈라 읽은 글자('叶' → '卩十')는 이름 사전으로 후보를 좁혀도 '霜叶' 와 '霜华' 처럼
여럿이 남는다. 후보 글자를 Windows 글꼴로 그려 화면 글자와 모양을 견준다. 게임 글꼴과 모양이 똑같지는
않지만 획 배치가 달라 충분히 갈린다.
"""
from __future__ import annotations

import functools
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .style import _otsu

FONTS = ("msyh.ttc", "simhei.ttf", "simsun.ttc")
GRID = 32           # 이 크기로 맞춰 견준다
MIN_SCORE = 0.5     # 이보다 덜 닮았으면 어느 후보도 아니다
MIN_MARGIN = 0.15   # 1등과 2등이 이만큼은 벌어져야 고른다 (갈라 읽힌 글자는 0.6 넘게 벌어진다)


@functools.lru_cache(maxsize=1)
def _font_path() -> str | None:
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in FONTS:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return None


def _normalize(ink: np.ndarray) -> np.ndarray | None:
    """잉크 부분만 잘라 GRID 크기로 맞추고 살짝 흐린다 (획 굵기 차이를 덜 타게)."""
    ys, xs = np.nonzero(ink)
    if len(xs) < 4:
        return None
    crop = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    img = Image.fromarray((crop * 255).astype(np.uint8)).resize((GRID, GRID), Image.BILINEAR)
    arr = np.asarray(img.filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32)
    arr -= arr.mean()
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else None


def _screen_shape(crop: Image.Image, dark_text: bool) -> np.ndarray | None:
    gray = np.asarray(crop.convert("L"), dtype=np.float32)
    thr = _otsu(gray)
    return _normalize(gray < thr if dark_text else gray > thr)


@functools.lru_cache(maxsize=512)
def _char_shape(ch: str) -> np.ndarray | None:
    path = _font_path()
    if path is None:
        return None
    size = 64
    img = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(img).text((size // 2, size // 2), ch, fill=255, font=ImageFont.truetype(path, size))
    return _normalize(np.asarray(img) > 128)


def best_char(crop: Image.Image, chars: list[str], dark_text: bool) -> str | None:
    """crop 의 글자와 가장 닮은 후보. 확실하지 않으면 None."""
    seen = _screen_shape(crop, dark_text)
    if seen is None:
        return None
    scores = []
    for ch in dict.fromkeys(chars):
        shape = _char_shape(ch)
        if shape is not None:
            scores.append((float((seen * shape).sum()), ch))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < MIN_SCORE:
        return None
    if len(scores) > 1 and scores[0][0] - scores[1][0] < MIN_MARGIN:
        return None
    return scores[0][1]


def match_word(frame: Image.Image, area: tuple, words, min_score: float, min_margin: float,
               with_score: bool = False):
    """area (x0, y0, x1, y1) 안의 글자를 후보 낱말들과 한 자씩 견준다. (낱말, 글자 상자) 또는 확실하지 않으면 None.

    바탕보다 밝은 글자든 어두운 글자든 적은 쪽을 글자로 본다. 글자 수는 잉크 폭 ÷ 글자 높이로 어림잡고
    (후보가 모두 같은 길이면 그 길이), 잉크를 글자 수만큼 고르게 나눠 견준다. 닮은 정도는 모든 글자의 평균이
    min_score 를 넘어야 하고, 1등과 2등의 차이는 후보끼리 다른 자리('自动回复'·'攻击回复' 의 앞 두 자)만으로 잰다.
    같은 '回复' 까지 섞어 평균하면 차이가 반으로 줄어 고르지 못한다. with_score 면 (낱말, 상자, 평균 점수).
    """
    x0, y0 = max(0, area[0]), max(0, area[1])
    if area[2] - x0 < 4 or area[3] - y0 < 4:
        return None
    gray = np.asarray(frame.crop((x0, y0, area[2], area[3])).convert("L"), dtype=np.float32)
    ink = gray > _otsu(gray)
    if ink.mean() > 0.5:
        ink = ~ink
    ys, xs = np.nonzero(ink)
    if len(xs) < 20:
        return None         # 빈 칸
    ix0, ix1, iy0, iy1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    lengths = {len(w) for w in words}
    n = lengths.pop() if len(lengths) == 1 else max(1, min(6, round((ix1 - ix0) / max(1, iy1 - iy0))))
    cands = [w for w in dict.fromkeys(words) if len(w) == n]
    if not cands:
        return None
    differ = [i for i in range(n) if len({w[i] for w in cands}) > 1] or list(range(n))
    cells = []
    for i in range(n):
        cells.append(_normalize(ink[iy0:iy1, ix0 + (ix1 - ix0) * i // n: ix0 + (ix1 - ix0) * (i + 1) // n]))
    scored = []
    for word in cands:
        each = []
        for cell, ch in zip(cells, word):
            shape = _char_shape(ch)
            each.append(float((cell * shape).sum()) if cell is not None and shape is not None else 0.0)
        scored.append((sum(each[i] for i in differ) / len(differ), sum(each) / n, word))
    scored.sort(reverse=True)
    if scored[0][1] < min_score:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < min_margin:
        return None
    box = (x0 + ix0, y0 + iy0, ix1 - ix0, iy1 - iy0)
    return (scored[0][2], box, scored[0][1]) if with_score else (scored[0][2], box)


def scores(crop: Image.Image, chars: list[str], dark_text: bool) -> list[tuple[float, str]]:
    """시험용: 후보마다 닮은 정도."""
    seen = _screen_shape(crop, dark_text)
    if seen is None:
        return []
    return sorted(((float((seen * _char_shape(ch)).sum()), ch) for ch in chars), reverse=True)
