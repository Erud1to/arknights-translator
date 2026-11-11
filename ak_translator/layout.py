"""OCR 줄들을 문단으로 묶는다.

한 줄씩 따로 번역하면 문장이 잘려 번역 품질이 크게 떨어진다. 그렇다고 다 묶으면
제목과 본문, 화자 이름과 대사가 한 덩어리가 되어 원본의 글자 크기·색 계층이 사라진다.

그래서 네 가지가 모두 맞을 때만 합친다.
  - 세로로 가깝다 (줄간격이 문단 안 간격 수준)
  - 가로로 겹친다
  - 글자 높이가 비슷하다 (제목 vs 본문 구분)
  - 글자색이 비슷하다 (화자 이름 vs 대사 구분)
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from .ocr import TITLE_RATIO, OCRLine

DIGIT_RATIO = 1.15  # 숫자만 있는 조각과 글자 조각의 크기가 이만큼 다르면 잇지 않는다 (merge_fragments).
                    # 스킬 아이콘 숫자 42px 와 밑줄 친 설명 줄 35px (1.2배) 를 갈라야 했다
DIGIT_MARKS = set(" +-.%/:×xX")
WRAP_MIN_CHARS = 10  # 글자색이 달라도 접힌 문장으로 이을 앞 줄의 최소 글자 수 (group_lines)
ICON_GAP = 2.6      # 한자로 끝난 조각과 숫자로 시작하는 조각 사이에 아이콘 하나가 낀 간격 (글자 높이 배수)
TOUCH_GAP = 0.5   # 같은 줄 조각 사이가 글자 높이의 이만큼 이하면 붙어 있는 것이다 (색이 달라도 잇는다)
CHIP_COLOR = 45   # 이웃한 글자 둘레 바탕색이 이만큼 넘게 다르면 다른 꼬리표·칸이다 (꼬리표끼리 52~119)
CHIP_FLAT = 20    # 글자 위아래 띠의 색 표준편차가 이보다 작아야 꼬리표처럼 고른 바탕이다
FAR_GAP = 2.5     # 한 줄 안 글자 사이가 글자 높이의 이만큼 넘게 벌어지면 따로 떨어진 글이다


def split_chips(lines: list[OCRLine], image) -> list[OCRLine]:
    """나란한 꼬리표를 한 줄로 읽은 것을 바탕색이 바뀌는 곳에서 가른다.

    스킬의 '自动回复'(초록) '手动触发'(회색) '⏱18秒'(진회색) 꼬리표가 '自动回复]手动触发018秒' 한 줄로 읽혀,
    번역문 하나가 세 꼬리표에 걸쳐 그려지며 겹쳤다. 낱자마다 바로 위아래 바탕색을 보고, 이웃 낱자와 크게
    다르면 끊는다. 꼬리표 경계(']', 'i')와 시계 아이콘('0', '．')을 읽은 낱자는 resolver 가 떼어 낸다.
    """
    arr = None
    out: list[OCRLine] = []
    for line in lines:
        if len(line.words) < 2 or not _has_ideograph(line.text):
            out.append(line)
            continue
        if arr is None:
            src = getattr(image, "_arr", None)
            arr = src[:, :, 2::-1] if src is not None else np.asarray(image.convert("RGB"))
        # 꼬리표 경계를 읽은 좁은 낱자(']', 'i')는 바탕색 기준으로 쓰지 않는다 (경계선 색이 묻어난다)
        colors = [_ring_color(arr, w) if w[3] >= w[4] * 0.5 else None for w in line.words]
        # 양쪽 다 고른 바탕(꼬리표)일 때만 끊는다. 그림 위의 이름('赤霄·绝影-驰')은 바탕이 고르지 않아 끊지 않는다.
        # 사이에 낀 아이콘(시계 '0', '．')은 바탕이 고르지 않으므로 건너뛰고 앞뒤 글자끼리 견주어, 아이콘은 뒤 꼬리표에 붙인다
        # 꼬리표 경계는 글자 사이 빈칸에서 바탕이 한두 픽셀 만에 확 바뀐다. 그러데이션·그림 바탕은 서서히 바뀐다
        cuts, last = [], None
        for i, color in enumerate(colors):
            if color is None:
                continue
            if (last is not None and _color_distance(colors[last], color) > CHIP_COLOR
                    and _sharp_edge(arr, line.words[last:i + 1])):
                cuts.append(last + 1)
            last = i
        # 글자 사이가 아주 멀면 OCR 이 따로 떨어진 것을 한 줄로 이은 것이다
        # ('收藏品' 과 189px 떨어진 헛것 '．一“012' 가 한 줄이 되어 옆 '零件箱' 블록과 합쳐졌다)
        heights = sorted(w[4] for w in line.words if _has_ideograph(w[0])) or [line.h]
        far = heights[len(heights) // 2] * FAR_GAP
        cuts += [i for i in range(1, len(line.words))
                 if line.words[i][1] - (line.words[i - 1][1] + line.words[i - 1][3]) > far]
        cuts = sorted(set(cuts))
        if not cuts:
            out.append(line)
            continue
        bounds = [0] + cuts + [len(line.words)]
        for a, b in zip(bounds, bounds[1:]):
            part = line.words[a:b]
            text = _words_text(part)
            if not any(ch.isalnum() for ch in text):
                continue    # 꼬리표 경계만 읽은 조각
            x0, y0 = min(w[1] for w in part), min(w[2] for w in part)
            x1, y1 = max(w[1] + w[3] for w in part), max(w[2] + w[4] for w in part)
            out.append(OCRLine(text=text, box=(x0, y0, x1 - x0, y1 - y0), conf=line.conf, words=part))
    return out


GAP_FILL_MIN = 0.6   # 같은 줄 두 조각 사이가 글자 높이의 이만큼 넘게 비면 글자를 놓쳤을 수 있다
GAP_FILL_MAX = 4     # 놓친 글자를 이만큼까지 채워 본다


def fill_gaps(lines: list[OCRLine], glob) -> list[OCRLine]:
    """같은 줄에서 가운데 글자를 놓친 것('一次性' … '背包')을 게임 데이터로 메운다.

    OCR 은 아이콘 그림 위의 글자를 자주 놓친다. 통합전략 장비 '一次性喷气背包' 는 '一次性' 과 '背包' 로 읽혀
    둘 다 번역되고 가운데 '喷气' 는 원문 그대로 남았다. 빈 폭만큼의 글자 수로 게임 글을 찾아('一次性??背包')
    딱 하나만 맞을 때 그 글로 잇는다. glob(꼴) 은 (키, 원문) 목록을 준다.
    """
    from . import textnorm as tn

    rows: list[list[int]] = []
    for i in sorted(range(len(lines)), key=lambda i: (lines[i].y, lines[i].x)):
        line = lines[i]
        for row in rows:
            ref = lines[row[-1]]
            if (abs((ref.y + ref.h / 2) - (line.y + line.h / 2)) < min(ref.h, line.h) * 0.3
                    and 0.8 <= line.h / max(1, ref.h) <= 1.25):
                row.append(i)
                break
        else:
            rows.append([i])
    drop: set[int] = set()
    out = list(lines)
    for row in rows:
        row.sort(key=lambda i: lines[i].x)
        for a, b in zip(row, row[1:]):
            if a in drop:
                continue
            la, lb = lines[a], lines[b]
            h = max(1, min(la.h, lb.h))
            gap = lb.x - (la.x + la.w)
            if not (h * GAP_FILL_MIN <= gap <= h * (GAP_FILL_MAX + 0.6)):
                continue
            ka, kb = tn.key(la.text), tn.key(lb.text)
            if not (_has_ideograph(ka) and _has_ideograph(kb)):
                continue
            guess = max(1, round(gap / (h * 1.05)))
            found = {}
            for n in range(max(1, guess - 1), min(GAP_FILL_MAX, guess + 1) + 1):
                for k, zh in glob(ka + "?" * n + kb):
                    found[k] = zh
            if len(found) != 1:
                continue
            zh = next(iter(found.values()))
            x0, y0 = la.x, min(la.y, lb.y)
            x1, y1 = lb.x + lb.w, max(la.y + la.h, lb.y + lb.h)
            out[a] = OCRLine(text=tn.strip_rich(zh), box=(x0, y0, x1 - x0, y1 - y0), conf=min(la.conf, lb.conf),
                             words=la.words + lb.words)
            drop.add(b)
    return [line for i, line in enumerate(out) if i not in drop]


def _sharp_edge(arr: np.ndarray, words) -> bool:
    """첫 낱자와 마지막 낱자 사이에서 바탕색이 이웃한 세로줄끼리 확 바뀌는가 (꼬리표 경계).

    사이의 빈칸과, 꼬리표 경계를 읽은 좁은 낱자(']', 'i')는 보고, 넓은 낱자(아이콘·글자) 자리는 뺀다.
    """
    H, W = arr.shape[:2]
    y0 = max(0, min(w[2] for w in words) + 2)
    y1 = min(H, max(w[2] + w[4] for w in words) - 2)
    first, last = words[0], words[-1]
    x0, x1 = max(0, first[1] + first[3]), min(W, last[1])
    if y1 - y0 < 4 or x1 - x0 < 2:
        return False
    keep = np.ones(x1 - x0, dtype=bool)
    for _t, wx, _wy, ww, wh in words[1:-1]:
        if ww >= wh * 0.5:
            keep[max(0, wx - x0):max(0, wx + ww - x0)] = False
    cols = np.median(arr[y0:y1, x0:x1].astype(np.float32), axis=0)
    jumps = np.linalg.norm(np.diff(cols, axis=0), axis=1)
    jumps[~(keep[1:] & keep[:-1])] = 0      # 뺀 자리를 사이에 둔 두 줄은 견주지 않는다
    return len(jumps) > 0 and float(jumps.max()) > CHIP_COLOR


def _ring_color(arr: np.ndarray, word) -> tuple | None:
    """낱자 바로 위아래 1~3px 띠의 중간 색. 글자에 닿지 않는 바탕을 본다.

    꼬리표는 글자 위아래 여백이 3px 남짓이라 띠를 더 멀리 잡으면 꼬리표 밖이 섞인다. 위아래 띠가 둘 다
    고르고 서로 같은 색이어야 꼬리표 바탕으로 본다. 그림 바탕은 대개 어느 한쪽이 고르지 않다.
    """
    _t, x, y, w, h = word
    H, W = arr.shape[:2]
    x0, x1 = max(0, x), min(W, x + max(w, 1))
    found = []
    for rows in ((max(0, y - 3), y), (y + h, min(H, y + h + 3))):
        pix = arr[rows[0]:rows[1], x0:x1].reshape(-1, 3)
        if len(pix) < 3 or float(pix.astype(np.float32).std(axis=0).max()) > CHIP_FLAT:
            return None
        found.append(tuple(float(v) for v in np.median(pix, axis=0)))
    if _color_distance(found[0], found[1]) > CHIP_FLAT:
        return None
    return tuple(int((a + b) / 2) for a, b in zip(found[0], found[1]))


def _words_text(words) -> str:
    out = ""
    for t, *_ in words:
        if out and out[-1].isascii() and out[-1].isalpha() and t[:1].isascii() and t[:1].isalpha():
            out += " "
        out += t
    return out

_CJK_END = "。．.!！?？」』〉》〕】〗”\"’'"


def _has_ideograph(text: str) -> bool:
    return any("㐀" <= ch <= "鿿" for ch in text)


def _digits_only(text: str) -> bool:
    """숫자(와 +·%·/ 같은 숫자 기호)만 있는 조각. 두 자리 이상."""
    t = text.strip()
    return sum(ch.isdigit() for ch in t) >= 2 and all(ch.isdigit() or ch in DIGIT_MARKS for ch in t)


def _numeric(text: str) -> bool:
    """숫자 위주의 줄인가 (오퍼레이터 카드의 레벨 '60', 아이콘을 잘못 읽은 '60冂')."""
    digits = sum(ch.isascii() and ch.isdigit() for ch in text)
    ideographs = sum("㐀" <= ch <= "鿿" for ch in text)
    if digits >= 1 and ideographs <= 1 and digits >= ideographs:
        return True
    # 레벨 옆 아이콘이 한자 몇 개로 읽히기도 한다 ('79力肀怕')
    return text[:2].isascii() and text[:2].isdigit() and ideographs <= 3


@dataclass
class Block:
    """번역·표시의 단위. 원본 줄 여러 개가 합쳐진 결과일 수 있다."""

    text: str
    box: tuple[int, int, int, int]
    rows: int = 1
    line_h: int = 0
    ink_h: int = 0  # 원문 글자 획의 실제 높이 (번역문 크기를 맞추는 기준)
    conf: float = 1.0
    members: list[OCRLine] = field(default_factory=list)
    style: object | None = None  # style.BlockStyle (첫 줄에서 가져온 색 정보)
    masked: str = ""      # 반쪽으로 갈라 읽힌 글자를 '?' 로 바꾼 글자 (한 줄짜리만)
    unknown: tuple = ()   # 그 '?' 자리들의 상자


def _is_cjk(ch: str) -> bool:
    return "぀" <= ch <= "鿿" or "＀" <= ch <= "￯"


def _join(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    if _is_cjk(a[-1]) or _is_cjk(b[0]):
        return a + b
    return a + " " + b


def _overlap_ratio(a: OCRLine, b: OCRLine) -> float:
    left = max(a.x, b.x)
    right = min(a.x + a.w, b.x + b.w)
    if right <= left:
        return 0.0
    return (right - left) / max(1, min(a.w, b.w))


_SENTENCE_END = "。．.!！?？…~～"


def _looks_unfinished(text: str) -> bool:
    """이 줄이 문장 도중에 끊긴 것처럼 보이는가 (마침표 없이 끝났는가)."""
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] not in _SENTENCE_END


def _color_distance(c1, c2) -> float:
    if not c1 or not c2:
        return 0.0
    return sum((int(a) - int(b)) ** 2 for a, b in zip(c1, c2)) ** 0.5


def _size_of(line: OCRLine, styles: list | None, idx: int) -> int:
    if styles is not None:
        st = styles[idx]
        if st is not None and getattr(st, "ink_h", 0) >= 6:
            return int(st.ink_h)
    return max(6, line.line_h)


def merge_fragments(
    lines: list[OCRLine],
    styles: list | None = None,
    gap_ratio: float = 1.2,
    height_tol: float = 0.35,
    color_tol: float = 90.0,
):
    """한 줄이 가로로 여러 조각으로 잡힌 것을 다시 이어 붙인다.

    OCR 은 「第三章 — 忘れられた都市」처럼 사이가 벌어진 제목을 조각으로 끊어 놓는 일이
    잦다. 그대로 두면 조각마다 따로 번역되어 문장이 망가진다.
    """
    if len(lines) <= 1:
        return list(lines), (list(styles) if styles is not None else None)

    order = sorted(range(len(lines)), key=lambda i: (lines[i].y, lines[i].x))
    rows: list[dict] = []
    for idx in order:
        line = lines[idx]
        top, bot = line.y, line.y + line.h
        center = (top + bot) / 2
        placed = False
        for row in rows:
            # 같은 줄인가: 짧은 쪽이 긴 쪽의 세로 범위 안에 들어오는가.
            # 높이가 같은지로 보면 「—」·「ー」 같은 납작한 글자를 놓친다.
            inter = min(row["bot"], bot) - max(row["top"], top)
            if inter <= 0 or inter < min(row["h"], line.h) * 0.7:
                continue
            if abs(row["center"] - center) > max(row["h"], line.h) * 0.35:
                continue

            # 행 전체 범위와 견준다. y 순으로 돌기 때문에 사이에 낀 조각이
            # 마지막에 올 수 있어, 마지막 요소만 보면 놓친다.
            ref_h = max(1, min(row["h"], line.h))
            if line.x >= row["x1"]:
                gap = line.x - row["x1"]
                left_text = max((lines[i] for i in row["idx"]), key=lambda m: m.x + m.w).text
                right_text = line.text
            elif line.x + line.w <= row["x0"]:
                gap = row["x0"] - (line.x + line.w)
                left_text = line.text
                right_text = min((lines[i] for i in row["idx"]), key=lambda m: m.x).text
            else:
                gap = 0  # 가로로 겹쳐 있으면 같은 줄로 본다
                left_text = right_text = ""
            limit = ref_h * gap_ratio
            # 글 사이에 화폐 아이콘이 낀 '是否消耗 [오리지늄각뿔] 16购买…？' 는 아이콘 폭만큼 벌어져도 한 문장이다.
            # PaddleOCR 은 아이콘 자리에서 줄을 끊는다
            if left_text.rstrip()[-1:] and _has_ideograph(left_text.rstrip()[-1]) and right_text.lstrip()[:1].isdigit():
                limit = ref_h * ICON_GAP
            if gap > limit:
                continue
            # 크기가 크게 다른 한자 조각은 잇지 않는다 (로딩 화면의 큰 제목 '炎国' 과 본문).
            # 대시처럼 한자가 없는 조각은 원래 납작하므로 크기를 보지 않는다
            first = row["idx"][0]
            ha, hb = _size_of(lines[first], styles, first), _size_of(line, styles, idx)
            if (_has_ideograph(lines[first].text) and _has_ideograph(line.text)
                    and max(ha, hb) >= min(ha, hb) * TITLE_RATIO):
                continue
            # 숫자만 있는 조각은 납작하지 않으니 크기가 다르면 옆 아이콘의 숫자다. PaddleOCR 은 스킬 아이콘의
            # 코스트 '38'·'45' 를 읽어, 바로 옆 설명 줄 '过载：…' 에 '38445过载：…' 로 붙었다.
            # 같은 크기의 강조 숫자('攻击力+135%')는 그대로 잇는다
            if (_digits_only(lines[first].text) != _digits_only(line.text)
                    and max(ha, hb) >= min(ha, hb) * DIGIT_RATIO):
                continue
            # 색이 다르면 다른 글자로 본다. 다만 거의 붙어 있으면 한 문장 안의 강조색이다
            # (스킬 설명 '攻击力+135%，攻击变为法术伤' 의 파란 '+135%' 때문에 한 줄이 둘로 갈려 번역문이 겹쳤다)
            if styles is not None:
                sa, sb = styles[row["idx"][-1]], styles[idx]
                if sa is not None and sb is not None:
                    if gap > ref_h * TOUCH_GAP and _color_distance(sa.fg, sb.fg) > color_tol:
                        continue
                    # 바탕이 다르면 붙어 있어도 다른 칸이다 (초록 '自动回复' · 회색 '手动触发' 꼬리표)
                    if _color_distance(sa.bg, sb.bg) > CHIP_COLOR:
                        continue

            row["idx"].append(idx)
            row["x0"] = min(row["x0"], line.x)
            row["x1"] = max(row["x1"], line.x + line.w)
            row["top"] = min(row["top"], top)
            row["bot"] = max(row["bot"], bot)
            row["h"] = row["bot"] - row["top"]
            row["center"] = (row["top"] + row["bot"]) / 2
            placed = True
            break
        if not placed:
            rows.append({
                "idx": [idx], "x0": line.x, "x1": line.x + line.w,
                "top": top, "bot": bot, "h": line.h, "center": center,
            })
    rows = [r["idx"] for r in rows]

    out_lines: list[OCRLine] = []
    out_styles: list = [] if styles is not None else None
    for row in rows:
        members = [lines[i] for i in row]
        if len(members) == 1:
            out_lines.append(members[0])
            if out_styles is not None:
                out_styles.append(styles[row[0]])
            continue
        members.sort(key=lambda m: m.x)
        text = ""
        for m in members:
            text = _join(text, m.text.strip())
        x0 = min(m.x for m in members)
        y0 = min(m.y for m in members)
        x1 = max(m.x + m.w for m in members)
        y1 = max(m.y + m.h for m in members)
        heights = sorted(m.line_h for m in members)
        masked = ""
        if any(m.masked for m in members):
            for m in members:
                masked = _join(masked, (m.masked or m.text).strip())
        out_lines.append(
            OCRLine(
                text=text,
                box=(x0, y0, x1 - x0, y1 - y0),
                conf=sum(m.conf for m in members) / len(members),
                line_h=heights[len(heights) // 2],
                masked=masked,
                unknown=tuple(u for m in members for u in m.unknown),
            )
        )
        if out_styles is not None:
            picked = [styles[i] for i in row if styles[i] is not None]
            if picked:
                # 대시 같은 납작한 조각이 글자 크기 기준을 끌어내리지 않게 거른다
                inks = sorted(s.ink_h for s in picked if s.ink_h >= 5)
                inks = inks or sorted(s.ink_h for s in picked)
                base = max(picked, key=lambda s: s.ink_h)
                out_styles.append(replace(base, ink_h=inks[len(inks) // 2]))
            else:
                out_styles.append(None)
    return out_lines, out_styles


def group_lines(
    lines: list[OCRLine],
    styles: list | None = None,
    # CJK 본문의 줄 간격은 보통 글자 높이의 1.5~1.8배다. 즉 글자 사이 빈틈은
    # 0.5~0.8배. 여기를 0.55 로 잡았더니 한 문장이 여러 줄로 접힌 설명문이
    # 줄마다 따로 번역됐다. 문단 사이 간격은 1.5배 이상이라 0.9 로도 충분히 갈린다.
    # (제목/본문 구분은 실제 획 높이와 글자색이 맡는다)
    gap_ratio: float = 0.9,
    min_overlap: float = 0.30,
    height_tol: float = 0.20,
    color_tol: float = 90.0,
    join_fragments: bool = True,
    image=None,
) -> list[Block]:
    """인접하고 성격이 같은 줄들을 문단으로 묶는다.

    styles 를 주면 (lines 와 같은 길이) 글자색까지 비교한다. image 를 주면 위아래 줄 사이 빈칸에
    테두리가 있는지 본다 (스킬 이름과 그 아래 '🔍范围' 버튼처럼 다른 칸의 글자를 묶지 않으려고).
    """
    if not lines:
        return []
    if join_fragments:
        lines, styles = merge_fragments(lines, styles, color_tol=color_tol)

    indexed = sorted(range(len(lines)), key=lambda i: (lines[i].y, lines[i].x))
    groups: list[list[int]] = [[indexed[0]]]

    def joins(prev_idx: int, idx: int) -> bool:
        prev, line = lines[prev_idx], lines[idx]
        ref_h = max(1, min(prev.line_h, line.line_h))
        vgap = line.y - (prev.y + prev.h)

        # 글자 크기 비교는 OCR 상자보다 실제 획 높이가 훨씬 안정적이다
        ha = _size_of(prev, styles, prev_idx)
        hb = _size_of(line, styles, idx)

        overlap = _overlap_ratio(prev, line)
        # 접힌 문장이면 줄간격이 조금 넓어도 이어 붙인다. 접힌 문장은
        #   - 앞 줄이 문장 부호 없이 끝나고
        #   - 뒷 줄이 더 짧다 (자리가 모자라 넘어간 마지막 줄이라서)
        # 는 특징이 있다. 길이가 엇비슷하게 나란한 목록 항목과 이걸로 구분한다.
        limit = ref_h * gap_ratio
        wrapped = (
            overlap >= 0.6
            and _looks_unfinished(prev.text)
            and line.w < prev.w * 0.95
        )
        if wrapped:
            limit = ref_h * gap_ratio * 1.4
        # 접힌 문장이고 왼쪽 끝이 맞으면 글자색이 달라 보여도 잇는다. 그림 위의 회색 글자는 글자색을 잘못 재기 쉽다
        # (통합전략 선택지 설명 '干员降落时，…相当' / '于自身攻击力200%…' 이 따로 번역됐다)
        # 앞 줄이 짧으면 접힌 문장이 아니라 꼬리표다 ('获得1件收藏品' 아래 붉은 유물 이름 '·贴地炸' 이 묶였다)
        same_column = (wrapped and abs(prev.x - line.x) <= ref_h * 0.3
                       and len(prev.text.strip()) >= WRAP_MIN_CHARS)

        near = -ref_h * 0.6 <= vgap <= limit
        # 레벨 숫자는 바로 아래 이름과 가깝고 크기도 비슷하지만 다른 글이다
        if _numeric(prev.text) != _numeric(line.text):
            return False
        overlaps = overlap >= min_overlap
        same_size = abs(ha - hb) <= max(ha, hb) * height_tol
        same_color = True
        if styles is not None and not same_column:
            sa, sb = styles[prev_idx], styles[idx]
            if sa is not None and sb is not None:
                same_color = (
                    _color_distance(sa.fg, sb.fg) <= color_tol
                    and _color_distance(sa.bg, sb.bg) <= color_tol * 1.4
                )
        if not (near and overlaps and same_size and same_color):
            return False
        return image is None or not _edge_between(image, prev, line)

    for idx in indexed[1:]:
        # 게임 UI 는 카드가 가로로 나란해서 y 순으로 훑으면 옆 칸 줄이 사이에 낀다.
        # 그래서 바로 앞 묶음만 보지 않고 위쪽에 있는 묶음을 모두 살핀다.
        for group in reversed(groups):
            if joins(group[-1], idx):
                group.append(idx)
                break
        else:
            groups.append([idx])

    blocks: list[Block] = []
    for g in groups:
        members = [lines[i] for i in g]
        text = ""
        for m in members:
            text = _join(text, m.text.strip())
        x0 = min(m.x for m in members)
        y0 = min(m.y for m in members)
        x1 = max(m.x + m.w for m in members)
        y1 = max(m.y + m.h for m in members)
        heights = sorted(m.line_h for m in members)
        inks = sorted(_size_of(lines[i], styles, i) for i in g)
        blocks.append(
            Block(
                text=text,
                box=(x0, y0, x1 - x0, y1 - y0),
                rows=len(members),
                line_h=heights[len(heights) // 2],
                ink_h=inks[len(inks) // 2],
                conf=sum(m.conf for m in members) / len(members),
                members=members,
                style=(styles[g[0]] if styles is not None else None),
                masked=members[0].masked if len(members) == 1 else "",
                unknown=members[0].unknown if len(members) == 1 else (),
            )
        )
    blocks = _merge_overlapping(blocks)
    blocks.sort(key=lambda b: (b.box[1], b.box[0]))
    return blocks


GAP_EDGE = 40   # 빈칸의 고른 가로줄끼리 밝기가 이만큼 넘게 다르면 테두리·그림자가 지나간다
FLAT_ROW = 14   # 가로줄 안 밝기 표준편차가 이보다 작으면 고른 줄이다 (밑줄·그림 바탕은 고르지 않다)



def _edge_between(image, upper: OCRLine, lower: OCRLine) -> bool:
    """두 줄 사이 빈칸에 테두리·그림자가 지나가는가.

    버튼 테두리·그림자는 가로로 고른 줄이 위아래로 밝기가 확 바뀐다 (스킬 이름과 그 아래 '🔍范围' 버튼).
    한 문단의 줄 사이는 고른 줄이어도 밝기가 같고, 키워드 밑줄('缚地')이나 그림 바탕은 줄 자체가 고르지 않다.
    """
    x0, x1 = max(upper.x, lower.x), min(upper.x + upper.w, lower.x + lower.w)
    y0, y1 = upper.y + upper.h, lower.y
    if x1 - x0 < 4 or y1 - y0 < 2:
        return False
    gap = np.asarray(image.crop((x0, y0, x1, y1)).convert("L"), dtype=np.float32)
    flat = gap.std(axis=1) < FLAT_ROW
    if flat.sum() < 2:
        return False
    means = gap.mean(axis=1)[flat]
    return float(means.max() - means.min()) > GAP_EDGE


OVERLAP_MERGE = 0.3   # 두 블록 상자가 작은 쪽 넓이의 이만큼 넘게 겹치면 한 블록이다


def _area_overlap(a, b) -> int:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def _lines_touch(a: Block, b: Block) -> bool:
    """두 블록의 줄 가운데 서로 맞닿은 것이 있는가. 상자만 겹치고 줄은 멀리 떨어져 있으면 거짓이다.

    문단 상자 안에 다른 글(지도 위 '岩壳蟹群' 꼬리표)이 들어앉아도 그 글이 문단의 줄과 이어진 것은 아니다.
    같은 줄 자리에서 가로 간격이 FAR_GAP 안이거나, 줄 상자가 서로 겹쳐야 맞닿은 것이다.
    """
    for ma in a.members:
        for mb in b.members:
            small = min(ma.w * ma.h, mb.w * mb.h)
            if small and _area_overlap((ma.x, ma.y, ma.w, ma.h), (mb.x, mb.y, mb.w, mb.h)) >= small * OVERLAP_MERGE:
                return True
            line_h = max(1, min(ma.line_h, mb.line_h))
            inter = min(ma.y + ma.h, mb.y + mb.h) - max(ma.y, mb.y)
            if inter >= min(ma.h, mb.h) * 0.5:
                gap = max(ma.x, mb.x) - min(ma.x + ma.w, mb.x + mb.w)
                if gap <= line_h * FAR_GAP:
                    return True
    return False


def _merge_overlapping(blocks: list[Block]) -> list[Block]:
    """상자가 크게 겹치는 블록을 하나로 합친다.

    한 줄이 두 블록에 나뉘어 들어가면(윗문단의 마지막 줄 왼쪽 + 아랫덩어리의 첫 줄 오른쪽) 두 번역문이 서로 덮는다.
    줄들을 위에서 아래, 왼쪽에서 오른쪽 순서로 다시 이어 한 문단으로 만든다.
    """
    out = list(blocks)
    merged = True
    while merged:
        merged = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                small = min(a.box[2] * a.box[3], b.box[2] * b.box[3])
                if small and _area_overlap(a.box, b.box) >= small * OVERLAP_MERGE and _lines_touch(a, b):
                    out[i] = _combine(a, b)
                    del out[j]
                    merged = True
                    break
            if merged:
                break
    return out


def _combine(a: Block, b: Block) -> Block:
    members = sorted(a.members + b.members, key=lambda m: (m.y + m.h / 2, m.x))
    # 가운데 높이가 반 줄 안이면 같은 줄로 보고 왼쪽부터
    line_h = max(1, sorted(m.line_h for m in members)[len(members) // 2])
    rows: list[list[OCRLine]] = []
    for m in members:
        if rows and abs((rows[-1][0].y + rows[-1][0].h / 2) - (m.y + m.h / 2)) < line_h * 0.5:
            rows[-1].append(m)
        else:
            rows.append([m])
    text = ""
    for row in rows:
        for m in sorted(row, key=lambda m: m.x):
            text = _join(text, m.text.strip())
    x0, y0 = min(a.box[0], b.box[0]), min(a.box[1], b.box[1])
    x1 = max(a.box[0] + a.box[2], b.box[0] + b.box[2])
    y1 = max(a.box[1] + a.box[3], b.box[1] + b.box[3])
    top = a if a.box[1] <= b.box[1] else b
    return Block(text=text, box=(x0, y0, x1 - x0, y1 - y0), rows=len(rows), line_h=line_h,
                 ink_h=max(a.ink_h, b.ink_h), conf=min(a.conf, b.conf), members=members, style=top.style)
