"""번역문 자리의 원문 글자가 아직 그대로인지, 옮겨 갔는지, 사라졌는지 본다.

메인 화면처럼 배경(캐릭터)이 쉬지 않고 움직이는 화면에서는 번역문 자리의 픽셀이 늘 바뀐다. 픽셀 전체로
'낡았다' 를 판단하면 글자는 그대로인데 번역문이 지워졌다가 다음 OCR 에서 다시 뜨기를 되풀이한다.
글자 획 픽셀만 보면 배경이 움직여도 글자는 그대로다. 메인·터미널 화면은 마우스를 따라 통째로 30px 쯤
기우는데, 그때는 같은 글자를 근처에서 찾아 번역문을 따라 옮긴다 (OCR 을 기다리지 않는다).

밝기는 초록 채널로 본다 (캡처 배열에서 바로 꺼낼 수 있다).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .style import _otsu

DIFF = 40          # 글자 픽셀의 밝기가 이만큼 달라지면 그 점은 바뀐 것이다
SAME = 0.8         # 글자 픽셀의 이 비율 이상이 그대로면 같은 글자다 (다른 글자면 절반도 안 맞는다)
BG_INKED = 0.4     # 바탕 표본이 이 비율 넘게 글자색이면 맞춘 자리가 아니다 (흰 판 위에 우연히 맞은 것)
POINTS = 400       # 글자 픽셀 표본 수
BG_POINTS = 200
REACH = 40         # 글자가 옮겨 갔는지 이만큼까지 찾아본다
# 글자 획과 바탕의 밝기 차이가 처음의 이만큼은 남아 있어야 같은 글자다. 초록 칸의 흰 글자 '自动回复'(획 255, 바탕 195)
# 자리가 편성 화면의 밝은 회색 바탕(225)이 되었는데 획 밝기 차이 40 안쪽이라 '그대로' 로 보고 번역문이 남았다
KEEP_CONTRAST = 0.5


@dataclass
class Ink:
    ys: np.ndarray      # 글자 픽셀 (상자 기준 좌표)
    xs: np.ndarray
    vals: np.ndarray    # 그때의 밝기
    bys: np.ndarray     # 바탕 픽셀 표본
    bxs: np.ndarray
    thr: float
    dark: bool          # 어두운 글자
    contrast: float = 0.0   # 그때 획과 바탕의 밝기 차이 (가운데값)


def green(frame) -> np.ndarray:
    """화면 전체의 초록 채널. 캡처 프레임(BGRA 배열)이든 PIL 이미지든."""
    arr = getattr(frame, "_arr", None)
    if arr is not None:
        return arr[:, :, 1]
    return np.asarray(frame.convert("RGB"))[:, :, 1]


def capture(g: np.ndarray, box, dark: bool) -> Ink | None:
    """box 안 글자 획의 모양을 기억한다. 글자가 너무 적거나 많으면(가르기 실패) None."""
    x, y, w, h = box
    patch = g[max(0, y):y + h, max(0, x):x + w].astype(np.int16)
    if patch.size == 0 or y < 0 or x < 0:
        return None
    thr = _otsu(patch.astype(np.float32))
    mask = patch < thr if dark else patch > thr
    if not 0.02 <= mask.mean() <= 0.75:     # 굵은 장식 글자는 상자의 60% 넘게 찬다 (메인 화면 '基建')
        return None
    # 획 가장자리는 배경과 섞인 색이라 배경이 움직이면 같이 바뀐다. 상하좌우가 다 획인 속살만 본다
    core = mask.copy()
    core[1:, :] &= mask[:-1, :]
    core[:-1, :] &= mask[1:, :]
    core[:, 1:] &= mask[:, :-1]
    core[:, :-1] &= mask[:, 1:]
    if core.sum() >= 30:
        mask_pts = core
    else:
        mask_pts = mask     # 획이 아주 가늘면 속살이 없다
    # 바탕 표본도 획에서 떨어진 곳만 (획 둘레의 섞인 색은 글자색으로 셀 수 있다)
    near = mask.copy()
    near[1:, :] |= mask[:-1, :]
    near[:-1, :] |= mask[1:, :]
    near[:, 1:] |= mask[:, :-1]
    near[:, :-1] |= mask[:, 1:]
    ys, xs = np.nonzero(mask_pts)
    bys, bxs = np.nonzero(~near)
    pick = np.linspace(0, len(ys) - 1, min(POINTS, len(ys))).astype(int)
    bpick = np.linspace(0, len(bys) - 1, min(BG_POINTS, len(bys))).astype(int)
    vals, bvals = patch[ys[pick], xs[pick]], patch[bys[bpick], bxs[bpick]]
    contrast = abs(float(np.median(vals)) - float(np.median(bvals))) if len(bvals) else 0.0
    return Ink(ys[pick], xs[pick], vals, bys[bpick], bxs[bpick], float(thr), dark, contrast)


def locate(g: np.ndarray, box, ink: Ink) -> tuple[int, int] | None:
    """글자가 그 자리에 그대로면 (0, 0), 옮겨 갔으면 (dx, dy), 없어졌으면 None."""
    x, y = box[0], box[1]
    if _matches(g, x, y, ink):
        return 0, 0
    best = _search(g, x, y, ink, 2, (-REACH, REACH), (-REACH, REACH))
    if best is None:
        return None
    dx, dy = best
    fine = _search(g, x, y, ink, 1, (dx - 1, dx + 1), (dy - 1, dy + 1))
    if fine is None or not _matches(g, x + fine[0], y + fine[1], ink):
        return None
    return fine


def _matches(g: np.ndarray, x: int, y: int, ink: Ink) -> bool:
    h, w = g.shape
    ys, xs = ink.ys + y, ink.xs + x
    if ys.min() < 0 or xs.min() < 0 or ys.max() >= h or xs.max() >= w:
        return False
    same = (np.abs(g[ys, xs].astype(np.int16) - ink.vals) < DIFF).mean()
    if same < SAME:
        return False
    if not len(ink.bys):
        return True
    bg = g[ink.bys + y, ink.bxs + x]
    inked = (bg < ink.thr) if ink.dark else (bg > ink.thr)
    if inked.mean() > BG_INKED:
        return False
    if ink.contrast:
        now = abs(float(np.median(g[ys, xs])) - float(np.median(bg)))
        if now < ink.contrast * KEEP_CONTRAST:
            return False    # 획 자리와 바탕이 같은 색이 되었다 (글자가 사라지고 밋밋한 바탕만 남음)
    return True


def _search(g: np.ndarray, x: int, y: int, ink: Ink, step: int, xr, yr) -> tuple[int, int] | None:
    """글자 픽셀이 가장 많이 맞는 위치 (dx, dy). SAME 에 못 미치면 None."""
    h, w = g.shape
    dxs = np.arange(xr[0], xr[1] + 1, step)
    dys = np.arange(yr[0], yr[1] + 1, step)
    ys = ink.ys[None, None, :] + y + dys[:, None, None]
    xs = ink.xs[None, None, :] + x + dxs[None, :, None]
    inside = ((ys >= 0) & (ys < h)).all(axis=2, keepdims=True) & ((xs >= 0) & (xs < w)).all(axis=2, keepdims=True)
    vals = g[np.clip(ys, 0, h - 1), np.clip(xs, 0, w - 1)].astype(np.int16)
    score = (np.abs(vals - ink.vals) < DIFF).mean(axis=2)
    score[~inside[:, :, 0]] = 0.0
    j, i = np.unravel_index(int(score.argmax()), score.shape)
    if score[j, i] < SAME:
        return None
    return int(dxs[i]), int(dys[j])
