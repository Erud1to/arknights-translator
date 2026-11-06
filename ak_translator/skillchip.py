"""스킬 화면의 초록 칸 'SP 회복 방식' (自动回复·攻击回复·受击回复) 을 글자 모양으로 읽는다.

초록 바탕에 흰 글자라 Windows OCR 이 아예 못 읽거나 '-' 하나로 읽었다 (탈룰라 스킬 화면 세 칸 모두).
바로 오른쪽 회색 칸의 '自动触发'·'手动触发' 는 잘 읽히므로, 그 글자 상자에서 왼쪽으로 한 칸 떨어진 자리를
세 후보와 견준다 (glyph.match_word). 맞으면 그 글자를 읽은 블록으로 더해 공식 번역('자연 회복')이 뜨게 한다.
"""
from __future__ import annotations

from dataclasses import replace

from . import textnorm as tn

SP_TYPES = ("自动回复", "攻击回复", "受击回复")
TRIGGERS = ("自动触发", "手动触发")
# 초록 칸 글자 끝과 회색 칸 글자 첫머리 사이 (글자 높이 배수). 스킬 특화 화면은 1.1 (39px/35px),
# 오퍼레이터 상세 화면은 칸이 붙어 있어 0.9 (23px/26px). 여럿을 견줘 가장 닮은 쪽을 쓴다
GAPS = (0.85, 0.95, 1.1)
MIN_SCORE = 0.5
MIN_MARGIN = 0.05


def _char_box(block, start: int, count: int) -> tuple | None:
    """블록 글자 start 부터 count 자의 상자. 한 줄짜리이고 낱자 상자가 글자와 맞을 때만."""
    if block.rows != 1 or len(block.members) != 1 or not block.members[0].words:
        return None
    words = block.members[0].words
    chars = [(i, j, len(t)) for i, (t, *_r) in enumerate(words) for j in range(len(t)) if not t[j].isspace()]
    if len(chars) != sum(not ch.isspace() for ch in block.text):
        return None
    first = sum(not ch.isspace() for ch in block.text[:start])
    last = first + count - 1
    if last >= len(chars):
        return None
    (wa, ja, na), (wb, jb, nb) = chars[first], chars[last]
    _t, ax, _ay, aw, _ah = words[wa]
    _t, bx, _by, bw, _bh = words[wb]
    x0 = int(ax + aw * ja / na)
    x1 = int(round(bx + bw * (jb + 1) / nb))
    y0 = min(w[2] for w in words[wa:wb + 1])
    y1 = max(w[2] + w[4] for w in words[wa:wb + 1])
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def fix_blocks(frame, blocks: list) -> list:
    """'自动触发'·'手动触发' 블록 왼쪽의 SP 회복 칸을 읽어 블록으로 더한다. 이미 읽혔으면 그대로 둔다."""
    if frame is None:
        return blocks
    keys = [tn.key(b.text) for b in blocks]
    if not any(t in k for k in keys for t in TRIGGERS):
        return blocks
    out = list(blocks)
    for block, k in zip(blocks, keys):
        if any(sp in k for sp in SP_TYPES):
            continue
        trigger = next((t for t in TRIGGERS if t in block.text), None)
        at = block.text.find(trigger) if trigger else -1
        if at < 0 or at > 3:
            continue        # 꼬리표 앞에 헛것 서너 자까지만 ('-自动触发')
        box = _char_box(block, at, len(trigger))
        if box is None:
            continue
        x, y, w, h = box
        if any(_left_of(b.box, box) and any(sp in tn.key(b.text) for sp in SP_TYPES) for b in blocks):
            continue
        from . import glyph
        best = None
        for gap in GAPS:
            right = x - round(h * gap) + h // 4
            area = (right - w - h // 2, y - h // 8, right, y + h + h // 8)
            found = glyph.match_word(frame, area, SP_TYPES, MIN_SCORE, MIN_MARGIN, with_score=True)
            if found is not None and (best is None or found[2] > best[2]):
                best = found
        if best is not None:
            out.append(_block(frame, block, best[0], best[1]))
    return out


def _left_of(a: tuple, b: tuple) -> bool:
    """a 가 b 바로 왼쪽 같은 줄에 있는가."""
    return abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) < b[3] and b[0] - b[3] * 6 < a[0] < b[0]


def _block(frame, like, name: str, box: tuple):
    from . import style

    st = style.analyze(frame, box, box[3])
    return replace(like, text=name, box=box, rows=1, line_h=box[3], ink_h=getattr(st, "ink_h", 0) or box[3],
                   members=[], style=st, masked="", unknown=())
