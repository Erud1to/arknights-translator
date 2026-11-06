"""공개모집: 화면에 뜬 태그 가운데 ★4 이상이 확정되는 조합을 찾는다 (중국 서버 모집 목록 기준).

규칙 (gacha_table 의 recruitRarityTable · specialTagRarityTable · recruitDetail 안내문):
  - 모집 시간 9시간(7:40 이상)이면 ★3~★5 가 나온다. ★1·★2 는 안 나온다.
  - ★6 은 '高级资深干员' 태그를 고를 때만 나오고, 그때는 ★6 확정이다.
  - '资深干员' 을 고르면 (高级资深干员 없이) ★5 확정이다.
  - 고른 태그(1~3개)를 모두 가진 오퍼레이터 가운데서 나온다. 그 가운데 가장 낮은 별이 확정 별이다.

오퍼레이터 태그 = 직군 태그(近卫干员…) + 위치(近战位·远程位) + character_table 의 tagList + 별 태그(★5 资深干员,
★6 高级资深干员) + 성별(男性干员·女性干员, 프로필의 【性别】). 모집 가능한 오퍼레이터는 recruitDetail 의 이름 목록이다.
표는 DB 를 만들 때 recruit_ops · recruit_tags 에 넣는다 (build_db). 예전 DB 에 표가 없으면 이 기능은 꺼진다.
"""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass

from . import textnorm as tn

PROFESSION_TAGS = {
    "PIONEER": "先锋干员", "WARRIOR": "近卫干员", "TANK": "重装干员", "SNIPER": "狙击干员",
    "CASTER": "术师干员", "MEDIC": "医疗干员", "SUPPORT": "辅助干员", "SPECIAL": "特种干员",
}
POSITION_TAGS = {"MELEE": "近战位", "RANGED": "远程位"}
GENDER_TAGS = {"男": "男性干员", "女": "女性干员"}
TOP, SENIOR, ROBOT = "高级资深干员", "资深干员", "支援机械"
HOURS = 9               # 확정 계산은 9시간 모집 기준 (대부분 이렇게 쓴다)
GOOD = 4                # 이 별 이상이 확정되는 조합만 보여 준다
MAX_TAGS = 3            # 한 번에 고를 수 있는 태그 수
_RICH = re.compile(r"<[^>]*>")
_GENDER = re.compile(r"【代号】\s*([^\n]+?)\s*\n\s*【性别】\s*([^\n]+)")


@dataclass(frozen=True)
class Op:
    name: str
    rarity: int             # 1~6
    tags: frozenset


@dataclass(frozen=True)
class Combo:
    tags: tuple             # 고를 태그 (화면 순서)
    rarity: int             # 확정 별 (나올 수 있는 가장 낮은 별)
    ops: tuple              # 나올 수 있는 오퍼레이터 (별 높은 순)


# ---------------------------------------------------------------- DB 만들 때
def build_rows(gacha: dict, chars: dict, texts) -> tuple[list[tuple], list[tuple], dict]:
    """(recruit_ops 줄 [(이름, 별, 태그 JSON)], recruit_tags 줄 [(태그, id)], 규칙). texts 는 게임 글 (성별을 찾는다)."""
    gender = {}
    for text in texts:
        if "【性别】" in text:
            for name, sex in _GENDER.findall(text):
                gender.setdefault(name.strip(), sex.strip()[:1])
    by_name = {}
    for char_id, c in chars.items():
        if char_id.startswith("char_") and not c.get("isNotObtainable"):
            by_name.setdefault(c.get("name"), c)
    ops = []
    for name in recruitable_names(gacha.get("recruitDetail", "")):
        c = by_name.get(name)
        if c is None:
            continue
        rarity = _rarity(c.get("rarity"))
        tags = {PROFESSION_TAGS.get(c.get("profession"), ""), POSITION_TAGS.get(c.get("position"), "")}
        tags |= set(c.get("tagList") or [])
        tags |= {SENIOR} if rarity == 5 else {TOP} if rarity == 6 else set()
        tags.add(GENDER_TAGS.get(gender.get(name, ""), ""))
        tags.discard("")
        ops.append((name, rarity, json.dumps(sorted(tags), ensure_ascii=False)))
    tag_rows = [(t["tagName"], int(t["tagId"])) for t in gacha.get("gachaTags", [])]
    table = gacha.get("recruitRarityTable", {})
    nine = table.get(str(HOURS * 60)) or {"rarityStart": 2, "rarityEnd": 4}
    names = {int(t["tagId"]): t["tagName"] for t in gacha.get("gachaTags", [])}
    special = {names[int(k)]: v[0] + 1 for k, v in gacha.get("specialTagRarityTable", {}).items()
               if int(k) in names and v}
    rule = {"low": nine["rarityStart"] + 1, "high": nine["rarityEnd"] + 1, "special": special, "hours": HOURS}
    return ops, tag_rows, rule


def recruitable_names(detail: str) -> list[str]:
    """recruitDetail 안내문에서 모집 가능한 오퍼레이터 이름. '★★★' 줄 바로 다음 줄이 '이름 / 이름 …' 이다."""
    lines = _RICH.sub("", detail).replace("\\n", "\n").splitlines()
    out = []
    for star, names in zip(lines, lines[1:]):
        if re.fullmatch(r"\s*★+\s*", star):
            out += [n.strip() for n in names.split("/") if n.strip()]
    return out


def _rarity(value) -> int:
    if isinstance(value, int):
        return value + 1
    m = re.search(r"(\d)", str(value or ""))
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- 계산
class Pool:
    def __init__(self, ops: list[Op], tags: set[str], rule: dict):
        self.ops = ops
        self.tags = tags
        self.low, self.high = int(rule.get("low", 3)), int(rule.get("high", 5))
        self.special = {k: int(v) for k, v in (rule.get("special") or {}).items()}

    def combos(self, shown: list[str]) -> list[Combo]:
        """화면의 태그로 만들 수 있는 조합 가운데 ★GOOD 이상 확정인 것. 별 높은 순, 같으면 태그 적은 순.
        더 적은 태그로 같은 별이 확정되면 그 태그를 품은 큰 조합은 뺀다."""
        found = []
        for size in range(1, MAX_TAGS + 1):
            for subset in itertools.combinations(shown, size):
                combo = self._combo(subset)
                if combo and combo.rarity >= GOOD:
                    found.append(combo)
        found.sort(key=lambda c: (-c.rarity, len(c.tags), len(c.ops)))
        keep = []
        for combo in found:
            if not any(set(k.tags) < set(combo.tags) and k.rarity >= combo.rarity for k in keep):
                keep.append(combo)
        return keep

    def robot_combos(self, shown: list[str]) -> list[tuple[str, Op]]:
        """支援机械 과 함께 고르면 ★1 로봇 하나로 정해지는 태그: [(태그, 로봇)]. 모집 시간을 3:50 이하로 둘 때만 맞다."""
        if ROBOT not in shown:
            return []
        robots = [op for op in self.ops if ROBOT in op.tags and op.rarity == 1]
        out: list[tuple[str, Op]] = []
        for tag in shown:
            hit = [r for r in robots if tag != ROBOT and tag in r.tags]
            if len(hit) == 1 and all(hit[0] is not r for _t, r in out):
                out.append((tag, hit[0]))
        return out

    def _combo(self, subset: tuple) -> Combo | None:
        need = set(subset)
        if TOP in need:
            ops = [op for op in self.ops if need <= op.tags and op.rarity == 6]
        else:
            ops = [op for op in self.ops if need <= op.tags and self.low <= op.rarity <= self.high]
        if not ops:
            return None   # 맞는 오퍼레이터가 없으면 태그 없이 뽑힌다 (확정 아님)
        ops.sort(key=lambda op: (-op.rarity, op.name))
        rarity = min(op.rarity for op in ops)
        for tag, star in self.special.items():
            if tag in need and (tag == TOP or TOP not in need):
                rarity = max(rarity, star)
        return Combo(tuple(subset), rarity, tuple(ops))


def load(db) -> Pool | None:
    """DB 의 모집 표. 표가 없는 예전 DB 면 None."""
    data = db.recruit()
    if not data:
        return None
    rows, tags, rule = data
    ops = [Op(name, int(rarity), frozenset(json.loads(tags_json))) for name, rarity, tags_json in rows]
    return Pool(ops, set(tags), rule)


CHIP_MIN_SCORE = 0.55    # 태그 이름 글자들과 평균 이만큼은 닮아야 그 태그로 본다
CHIP_MIN_MARGIN = 0.06   # 1등 태그와 2등 태그가 이만큼은 벌어져야 고른다


def _clusters(values: list[float], gap: float) -> list[float]:
    """가까운 값끼리 묶은 무리의 평균 (작은 것부터)."""
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= gap:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(g) / len(g) for g in out]


def _nearest(centers: list[float], v: float) -> int:
    return min(range(len(centers)), key=lambda i: abs(centers[i] - v))


def _inside(box: tuple, area: tuple) -> bool:
    x, y, w, h = box
    ax, ay, aw, ah = area
    cx, cy = x + w / 2, y + h / 2
    return ax <= cx <= ax + aw and ay <= cy <= ay + ah


def _read_chip(frame, cx: int, top: int, h: int, width: int, tags: list[str]) -> tuple[str, tuple] | None:
    """태그 칸 한 칸의 글자를 태그 이름과 견준다. (태그, 글자 상자) 또는 확실하지 않으면 None.
    고른 태그 칸은 밝은 바탕에 어두운 글자라 glyph.match_word 가 적은 쪽을 글자로 본다."""
    from . import glyph

    x0, y0 = max(0, cx - width // 2), max(0, top - h // 3)
    return glyph.match_word(frame, (x0, y0, x0 + width, y0 + h + h * 2 // 3), tags, CHIP_MIN_SCORE, CHIP_MIN_MARGIN)


def _tag_block(frame, name: str, box: tuple):
    from . import layout, style

    st = style.analyze(frame, box, box[3])
    return layout.Block(text=name, box=box, rows=1, line_h=box[3], ink_h=getattr(st, "ink_h", 0) or box[3], style=st)


# ---------------------------------------------------------------- 화면
# 모집 화면의 글자. 하나라도 보이면 태그 셋으로도 모집 화면으로 본다 (없으면 넷 이상이 모여 있어야 한다)
MARKERS = ("招募时限", "职业需求", "公开招募")
COLORS = {6: "#FF8A3D", 5: "#FFD24D", 4: "#C59BFF"}     # 게임의 별 색 (주황·노랑·보라)
MUTED = "#AEB4BD"
MAX_LINES = 6           # 상자에 보여 줄 조합 수
MAX_NAMES = 4           # 조합마다 보여 줄 오퍼레이터 수


class Recruiter:
    """화면에 떠 있는 번역문(OCR 이 읽은 원문과 상자)에서 모집 태그를 찾아, 확정 조합의 태그 테두리와
    조합 목록 상자를 오버레이 항목으로 만든다. ko_of(중국어) → 한국어 표기 (태그·오퍼레이터 이름)."""

    def __init__(self, db, ko_of):
        self.pool = load(db)
        self.ko_of = ko_of
        self._keys = {tn.key(t): t for t in self.pool.tags} if self.pool else {}
        self._cache: tuple | None = None

    def items(self, shown: list[tuple]) -> dict[str, dict]:
        """shown: [(원문, 글자 상자, 바탕 가로 범위 또는 None)]. 모집 화면이 아니면 빈 dict."""
        if not self.pool:
            return {}
        boxes: dict[str, tuple] = {}     # 태그 → 칸 상자
        heights: dict[str, int] = {}     # 태그 → 글자 높이 (목록 글자 크기를 맞춘다)
        marker = False
        for src, box, room in shown:
            marker = marker or any(m in src for m in MARKERS)
            parts = self._tags_in(src, box)
            for name, part in parts:
                # 태그 칸 테두리는 글자가 아니라 칸(회색 바탕)을 두른다. 바탕 범위를 모르면 글자 둘레를 조금 넓힌다
                x, y, w, h = part
                x0, x1 = (room if room and len(parts) == 1 else (x - h // 2, x + w + h // 2))
                if name not in boxes:
                    boxes[name] = (x0, y - h * 2 // 5, x1 - x0, h + h * 4 // 5)
                    heights[name] = h
        if not self._is_screen(boxes, marker):
            return {}
        # 화면 순서 (위 줄부터, 왼쪽부터)로 놓아야 조합 태그가 화면에 보이는 차례로 적힌다
        shown_tags = sorted(boxes, key=lambda t: (boxes[t][1] // max(1, boxes[t][3]), boxes[t][0]))
        key = tuple((t, boxes[t]) for t in shown_tags)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        size = sorted(heights.values())[len(heights) // 2]
        out = self._render(shown_tags, boxes, size, self.pool.combos(shown_tags))
        self._cache = (key, out)
        return out

    def _tags_in(self, src: str, box: tuple) -> list[tuple[str, tuple]]:
        """이 번역문에 든 태그와 그 상자. OCR 이 칩 여럿을 한 줄로 읽었으면 글자 수 비율로 상자를 나눈다."""
        k = tn.key(src)
        if k in self._keys:
            return [(self._keys[k], box)]
        parts, i = [], 0
        while i < len(k):
            hit = next((k[i:j] for j in range(len(k), i, -1) if k[i:j] in self._keys), None)
            if hit is None:
                return []
            parts.append(hit)
            i += len(hit)
        x, y, w, h = box
        out, start = [], 0
        for part in parts:
            x0 = x + w * start // len(k)
            start += len(part)
            out.append((self._keys[part], (x0, y, x + w * start // len(k) - x0, h)))
        return out

    def fix_blocks(self, frame, blocks: list) -> list:
        """모집 화면이면 태그 칸 격자에서 OCR 이 못 읽었거나 잘못 읽은 칸을 글자 모양으로 태그 이름과 맞춘다.

        Windows OCR 은 태그 칩의 '治疗' 를 몇 배로 키워 읽혀도 '冫口了' 로 읽었다. 태그는 31개뿐이라, 읽힌 태그 칸으로
        격자(줄·칸 간격)를 잡고 빈 칸의 글자를 같은 글자 수의 태그 이름과 한 자씩 견준다 (glyph). 맞으면 그 태그를
        읽은 블록으로 바꿔 넣는다. 모집 화면이 아니거나 격자를 못 잡으면 그대로 돌려준다.
        """
        if not self.pool or frame is None:
            return blocks
        tagged = [b for b in blocks if tn.key(b.text) in self._keys]
        marker = any(m in b.text for b in blocks for m in MARKERS)
        if len(tagged) < (3 if marker else 4):
            return blocks
        h = sorted(b.box[3] for b in tagged)[len(tagged) // 2]
        rows = _clusters([b.box[1] for b in tagged], h / 2)
        cols = _clusters([b.box[0] + b.box[2] / 2 for b in tagged], h * 2)
        if len(cols) < 2 or len(rows) > 3:
            return blocks
        pitch = min(b - a for a, b in zip(cols, cols[1:]))
        taken = {(_nearest(rows, b.box[1]), _nearest(cols, b.box[0] + b.box[2] / 2)) for b in tagged}
        out = [b for b in blocks]
        for r, row_y in enumerate(rows):
            for c, col_x in enumerate(cols):
                if (r, c) in taken:
                    continue
                found = _read_chip(frame, int(col_x), int(row_y), h, int(pitch * 0.8), sorted(self.pool.tags))
                if found is None:
                    continue
                name, box = found
                # 그 칸 안에 OCR 이 읽어 둔 헛것('冫口了')은 뺀다. 같은 자리에 번역문이 둘 뜨지 않게
                out = [b for b in out if not _inside(b.box, (col_x - pitch / 2, row_y - h / 2, pitch, h * 2))
                       or tn.key(b.text) in self._keys]
                out.append(_tag_block(frame, name, box))
        return out

    @staticmethod
    def _is_screen(boxes: dict, marker: bool) -> bool:
        if len(boxes) < (3 if marker else 4):
            return False
        hs = sorted(b[3] for b in boxes.values())
        mid = hs[len(hs) // 2]
        if hs[-1] > mid * 1.8 or hs[0] < mid / 1.8:
            return False    # 크기가 제각각이면 한 화면의 태그 칸이 아니다
        xs = [b[0] for b in boxes.values()] + [b[0] + b[2] for b in boxes.values()]
        ys = [b[1] for b in boxes.values()] + [b[1] + b[3] for b in boxes.values()]
        return max(ys) - min(ys) <= mid * 6 and max(xs) - min(xs) <= mid * 30

    def _render(self, tags: list[str], boxes: dict, size: int, combos: list[Combo]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        best: dict[str, int] = {}
        for combo in combos:
            for t in combo.tags:
                best[t] = max(best.get(t, 0), combo.rarity)
        for t, star in best.items():
            color = COLORS.get(star, COLORS[4])
            out[f"recruit-mark:{t}"] = {"kind": "mark", "text": f"mark {color}", "box": boxes[t], "color": color}
        lines = [(f"공개모집 확정 조합 ({HOURS}시간)", MUTED)]
        for combo in combos[:MAX_LINES]:
            exact = all(op.rarity == combo.rarity for op in combo.ops)
            label = f"★{combo.rarity}" + ("" if exact else " 이상")
            names = [self.ko_of(op.name) for op in combo.ops[:MAX_NAMES]]
            more = len(combo.ops) - MAX_NAMES
            who = ", ".join(names) + (f" 외 {more}명" if more > 0 else "")
            tag_ko = " + ".join(self.ko_of(t) for t in combo.tags)
            lines.append((f"{label}   {tag_ko}   →  {who}", COLORS.get(combo.rarity, COLORS[4])))
        if len(combos) > MAX_LINES:
            lines.append((f"그 밖에 {len(combos) - MAX_LINES}개 조합", MUTED))
        if not combos:
            lines.append(("★4 이상이 확정되는 조합은 없습니다", MUTED))
        if ROBOT in tags:
            lines.append((f"{self.ko_of(ROBOT)}: 3:50 이하로 두면 ★1 로봇 확정", MUTED))
            picks = self.pool.robot_combos(tags)
            if picks:
                lines.append(("  " + " · ".join(f"+{self.ko_of(t)} → {self.ko_of(op.name)}" for t, op in picks),
                              MUTED))
        xs = [boxes[t][0] for t in tags] + [boxes[t][0] + boxes[t][2] for t in tags]
        ys = [boxes[t][1] for t in tags] + [boxes[t][1] + boxes[t][3] for t in tags]
        out["recruit-panel"] = {"kind": "panel", "text": "\n".join(text for text, _c in lines), "lines": lines,
                                "box": (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)), "line_h": size}
        return out
