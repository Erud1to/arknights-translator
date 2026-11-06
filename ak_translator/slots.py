"""오퍼레이터 카드 격자에서 OCR 이 놓친 한 글자 이름('宴', '锏', '陈')을 찾는다.

Windows OCR 은 글자 하나만 있는 줄을 자주 놓친다 (복제해 붙이거나 키워서 다시 읽혀도 '宀' 처럼
부수만 나온다). 오퍼레이터 목록의 이름은 카드 오른쪽 끝에 맞춰 같은 간격으로 늘어서 있으므로, 한 줄에서
읽힌 이름들의 오른쪽 끝으로 카드 간격을 재면 이름이 비어 있는 카드 자리를 알 수 있다. 그 자리에
글자 하나 크기의 글자가 있으면 한 글자 이름 후보(23개)와 모양을 견준다.
맞는 글자는 0.83~0.86, 다른 후보는 0.41 이하로 크게 갈린다.
"""
from __future__ import annotations

import numpy as np

from . import glyph
from .style import _otsu

ROW_MIN = 3          # 한 줄에서 이만큼은 이름이 읽혀야 카드 격자로 본다
MIN_PITCH = 3.0      # 카드 간격은 글자 높이의 3배는 넘는다
MIN_CONTRAST = 60    # 글자와 바탕의 밝기 차이
MIN_SCORE = 0.65
MIN_MARGIN = 0.25


def one_char_names(frame, blocks, names) -> list[tuple[str, tuple[int, int, int, int]]]:
    """(글자, 상자) 목록. names 는 NameIndex (읽힌 이름을 알아보고, 한 글자 이름 후보를 준다)."""
    chars = list(names.single)
    if not chars:
        return []
    anchors = [b for b in blocks if b.rows == 1 and names.is_name(b.text)]
    out = []
    for row in _rows(anchors):
        out += _scan_row(frame, row, blocks, chars)
    return out


def _rows(anchors) -> list[list]:
    """세로 위치와 글자 높이가 같은 이름끼리 한 줄로 묶는다."""
    rows: list[list] = []
    for b in sorted(anchors, key=lambda b: b.box[1] + b.box[3] / 2):
        cy, h = b.box[1] + b.box[3] / 2, b.box[3]
        last = rows[-1][-1] if rows else None
        if (last is not None and abs(last.box[1] + last.box[3] / 2 - cy) < h * 0.4
                and 0.75 <= h / max(1, last.box[3]) <= 1.33):
            rows[-1].append(b)
        else:
            rows.append([b])
    return [r for r in rows if len(r) >= ROW_MIN]


def _pitch(rights: list[int], h: int) -> float | None:
    """오른쪽 끝들이 일정한 간격의 정수배로 늘어서 있으면 그 간격."""
    diffs = [b - a for a, b in zip(rights, rights[1:])]
    if not diffs or min(diffs) < h * MIN_PITCH:
        return None
    base = min(diffs)
    steps = [round(d / base) for d in diffs]
    pitch = sum(diffs) / sum(steps)
    tol = max(4.0, h * 0.15)
    if any(abs(d - n * pitch) > tol for d, n in zip(diffs, steps)):
        return None
    return pitch


def _scan_row(frame, row, blocks, chars) -> list:
    hs = sorted(b.box[3] for b in row)
    h = hs[len(hs) // 2]
    y = sorted(b.box[1] for b in row)[len(row) // 2]
    rights = sorted(b.box[0] + b.box[2] for b in row)
    pitch = _pitch(rights, h)
    if pitch is None:
        return []
    dark = sum(1 for b in row if b.style is not None and b.style.dark_text) * 2 > len(row)
    out = []
    k = -int((rights[0] - h * 1.2) // pitch)
    while True:
        right = int(round(rights[0] + k * pitch))
        k += 1
        if right > frame.width:
            break
        slot = (int(right - h * 1.5), y, int(h * 1.5), h)
        if slot[0] < 0 or any(_overlaps(slot, b.box) for b in blocks):
            continue
        found = _read_slot(frame, right, y, h, dark, chars)
        if found:
            out.append(found)
    return out


def _read_slot(frame, right: int, y: int, h: int, dark: bool, chars):
    """카드 자리 오른쪽 끝에 붙은 글자 하나를 읽는다. 없거나 두 글자 이상이면 None."""
    x0, x1 = max(0, int(right - h * 3)), min(frame.width, int(right + h * 0.4))
    y0, y1 = max(0, int(y - h * 0.1)), min(frame.height, int(y + h * 1.1))
    if x1 - x0 < h or y1 - y0 < h * 0.8:
        return None
    gray = np.asarray(frame.crop((x0, y0, x1, y1)).convert("L"), dtype=np.float32)
    lo, hi = np.percentile(gray, (5, 95))
    if hi - lo < MIN_CONTRAST:
        return None
    ink = gray < _otsu(gray) if dark else gray > _otsu(gray)
    cols = np.nonzero(ink.sum(axis=0) >= 2)[0]
    near = cols[np.abs(cols + x0 - right) <= h * 0.3]
    if near.size == 0:
        return None
    # 오른쪽 끝에서 왼쪽으로, 글자 안의 틈('阿' 의 좌우 사이)보다 넓은 빈칸이 나올 때까지가 이름이다
    end = int(near.max())
    start = end
    for c in cols[::-1]:
        if c > end:
            continue
        if start - c > h * 0.35:
            break
        start = int(c)
    width = end - start + 1
    if not (h * 0.45 <= width <= h * 1.2) or start <= 1:
        return None
    rows = np.nonzero(ink[:, start:end + 1].sum(axis=1) >= 1)[0]
    if rows.size == 0 or not (h * 0.6 <= rows[-1] - rows[0] + 1 <= h * 1.25):
        return None
    top, bottom = int(rows[0]), int(rows[-1])
    crop = frame.crop((x0 + start - 3, y0 + top - 3, x0 + end + 4, y0 + bottom + 4))
    ranked = glyph.scores(crop, chars, dark)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_MARGIN:
        return None
    return ranked[0][1], (x0 + start, y, width, h)


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
