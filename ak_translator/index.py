"""조각 단위 색인 (실험). 화면에서 읽은 글이 공식·번역 묶음의 글 중 어느 구간에 해당하는지 찾아 그 한국어를 돌려준다.

지금의 규칙들(_within · _by_sentences · 한 글자 틀린 글 찾기 …)이 각자 하던 일을 한 방식으로 합치려는 시도다.
설계 (2026-11-10, 브랜치 index-matcher):
  1 후보   화면 글(T)에서 간격을 두고 6글자 닻을 뽑아 기존 SQLite 색인(fts, trigram)으로 닻이 든 글만 좁힌다.
           번역 묶음은 켤 때 만드는 작은 메모리 색인을 쓴다. DB 형식은 바뀌지 않는다.
  2 정렬   후보마다 닻 위치 둘레의 짧은 창에서 T 와 맞춘다 (difflib). T 의 몇 %가 덮였나(cover)·덮인 구간의 일치도(ident).
           OCR 이 글자 한두 개를 틀리게 읽어도 맞는다.
  3 고르기 cover·ident 가 기준 이상인 후보 중 T 를 덮는 가장 작은 글. 한 글로 안 덮이면 문장 2~5개씩 묶어 하나씩(이어 붙은
           공식 항목) 같은 방식으로 맞춘다. 하나라도 못 맞추면 None (그러면 기계 번역).
  4 자르기 글의 일부 구간만 맞았으면 중·한 문장 수가 같을 때 해당 문장만, 다르면 구간이 글의 60% 이상일 때만 통째로.
Resolver 가 cfg["matcher"]=="index"(기본)일 때 쓴다. "rules" 로 옛 규칙으로 돌아갈 수 있다.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
import threading
from dataclasses import dataclass

from . import textnorm as tn

MIN_KEY = 12            # 이보다 짧은 글은 맞는 글이 너무 많다
ANCHOR = 6              # 닻 길이 (글자)
ANCHORS = 6             # 닻 개수 상한
LIMIT = 300             # 후보 상한 (닻을 전부 맞춘 1차 질의)
LIMIT_LOOSE = 1000      # 닻 일부만 맞아도 되는 2차 질의에서 받아 올 상한. 받은 뒤 닻이 많이 든 순으로 KEEP 개만 맞춘다
KEEP = 40
COVER_MIN = 0.85        # T 의 이만큼은 덮여야 한다
IDENT_MIN = 0.85        # 덮은 구간의 일치도
IDENT_LOOSE = 0.75      # T 가 95% 이상 덮였을 때의 일치도 기준
STRICT_COVER = 0.9      # 문장 묶음을 하나씩 맞출 때
SCOPE_MIN = 0.4         # 글의 일부 구간만 맞았을 때 통째로 보이려면 구간이 글의 이 이상이어야 한다 (옛 규칙 WITHIN_RATIO 와 같게)
SLACK = 24              # 닻 둘레 창 여유 (글자)
MAX_KEY = 4000
CACHE = 512
SENT_SPAN = 5
# 문장 끝: 。！？ 뒤나, 말줄임표 바로 뒤에서 닫는 따옴표('……”')가 나오는 곳. 공식 항목은 '……”' 에서 끝나기도 한다
_SENT = re.compile(r"(?:[^。！？!?…]|…(?![”’」』）)]))*(?:[。！？!?]+[”’」』）)]*|…+[”’」』）)]+)|[^。！？!?]+$")
_KO_SENT = re.compile(r".*?[.!?。]+[\"'”’)」』]*(?:\s+|$)|.+$", re.S)


# 문장 맞추기 (Gale-Church): 번역하면서 문장을 나누거나 합쳐 중·한 문장 수가 다를 때, 길이 비율이 가장 그럴듯하게 이어지도록 짝을 짓는다
_ALIGN_MOVES = ((1, 1, 0.0), (1, 2, 0.5), (2, 1, 0.5), (2, 2, 0.9), (1, 3, 1.2), (3, 1, 1.2), (1, 0, 3.0), (0, 1, 3.0))


_QUOTE_OPEN = "“‘「『\"'"
_QUOTE_CLOSE = "”’」』\"'"


def _quote_flags(sents: list[str]) -> tuple[list[bool], list[bool]]:
    """문장마다 (따옴표로 시작하나, 따옴표로 끝나나). 대사가 어디서 나뉘는지가 길이보다 믿을 만한 단서다."""
    return ([s.lstrip()[:1] in _QUOTE_OPEN for s in sents], [s.rstrip()[-1:] in _QUOTE_CLOSE for s in sents])


def align_sentences(zl: list[int], kl: list[int], zq: tuple[list[bool], list[bool]] | None = None,
                    kq: tuple[list[bool], list[bool]] | None = None) -> list[tuple[int, int, int, int]] | None:
    """중국어 문장 길이들 zl 과 한국어 문장 길이들 kl 을 (z0, z1, k0, k1) 묶음으로 짝짓는다 (반열린 구간). 안 맞으면 None.
    zq·kq(따옴표 표시)가 있으면 묶음의 첫 문장이 따옴표로 열리는지, 끝 문장이 닫히는지가 서로 다를 때 벌점을 준다."""
    n, m = len(zl), len(kl)
    if not n or not m:
        return None
    ratio = sum(kl) / max(1, sum(zl))
    band = max(8, int(0.25 * max(n, m)))
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    zc, kc = [0], [0]
    for v in zl:
        zc.append(zc[-1] + v)
    for v in kl:
        kc.append(kc[-1] + v)
    for i in range(n + 1):
        centre = i * m / n
        for j in range(max(0, int(centre) - band), min(m, int(centre) + band) + 1):
            base = cost[i][j]
            if base == inf:
                continue
            for a, b, pen in _ALIGN_MOVES:
                ni, nj = i + a, j + b
                if ni > n or nj > m:
                    continue
                z, k = zc[ni] - zc[i], kc[nj] - kc[j]
                delta = (k - ratio * z) / (max(z, 4) * ratio * 3) ** 0.5
                c = base + delta * delta / 2 + pen
                if zq and kq and a and b:
                    c += 0.8 * (zq[0][i] != kq[0][j]) + 0.8 * (zq[1][ni - 1] != kq[1][nj - 1])
                if c < cost[ni][nj]:
                    cost[ni][nj] = c
                    back[ni][nj] = (i, j)
    if cost[n][m] == inf:
        return None
    out, i, j = [], n, m
    while (i, j) != (0, 0):
        prev = back[i][j]
        if prev is None:
            return None
        out.append((prev[0], i, prev[1], j))
        i, j = prev
    return out[::-1]


_LINE = re.compile(r"(?:\\n|\n)+")


def zh_sentences(zh: str) -> list[str]:
    """공식 중국어 글을 문장으로. 줄바꿈(문단·채팅 한 줄)은 문장 끝이다 ('……' 로 끝난 채팅 메시지가 다음 메시지와 한 문장이 되지 않게)."""
    return [s for line in _LINE.split(tn.strip_rich(zh)) for s in _SENT.findall(line) if tn.key(s)]


def ko_sentences(ko: str) -> list[str]:
    return [s.strip() for line in _LINE.split(tn.strip_rich(ko)) for s in _KO_SENT.findall(line) if s.strip()]


def _plausible(text: str, ks: list[str], zl: list[int], first: int, last: int) -> bool:
    """뽑은 한국어의 길이가 중국어 구간 길이에 이 글 전체의 한·중 길이 비율을 곱한 값과 크게 다르면 짝짓기가 엇나간 것이다
    (짧은 채팅 한 줄에 앞뒤 메시지·시각이 딸려 온 경우 등). 틀린 구간을 보이느니 못 맞췄다고 보고 기계 번역에 맡긴다."""
    expected = sum(len(re.sub(r"\s", "", k)) for k in ks) / max(1, sum(zl)) * sum(zl[first:last + 1])
    return 0.6 <= len(re.sub(r"\s", "", text)) / max(1.0, expected) <= 1.5


@dataclass
class Cover:
    ko: str
    zh: str
    via: str                # official / pack
    cover: float
    ident: float
    parts: int = 1


@dataclass
class _Cand:
    key: str
    zh: str
    ko: str
    via: str
    cover: float
    ident: float
    s0: int
    s1: int


def _anchors(key: str) -> list[str]:
    """key 에서 간격을 두고 뽑은 닻 (6글자). 앞·가운데·뒤에 고르게."""
    if len(key) <= ANCHOR:
        return [key]
    n = min(ANCHORS, max(1, len(key) // 10 + 1))
    if n == 1:
        return [key[(len(key) - ANCHOR) // 2:][:ANCHOR]]
    last = len(key) - ANCHOR
    return [key[round(i * last / (n - 1)):][:ANCHOR] for i in range(n)]


def _fts_query(anchors: list[str], loose: bool) -> str:
    """loose=False: 닻을 전부 가진 글 (정확히 든 글). loose=True: 닻 하나라도 가진 글 (OCR 이 틀리게 읽은 곳이 닻을 깨뜨렸을 때)."""
    groups = []
    for a in anchors:
        grams = dict.fromkeys(a[i:i + 3].replace('"', "") for i in range(len(a) - 2))
        groups.append("(" + " AND ".join(f'"{g}"' for g in grams if g) + ")")
    return (" OR " if loose else " AND ").join(groups)


def align(t: str, s: str, anchors: list[str]) -> tuple[float, float, int, int] | None:
    """(cover, ident, s0, s1). t 를 s 의 어느 구간에 맞췄나. 닻이 s 에 없으면 None."""
    pos = -1
    off = 0
    for a in anchors:
        pos = s.find(a)
        if pos >= 0:
            off = t.find(a)
            break
    if pos < 0:
        return None
    start = max(0, pos - max(0, off) - SLACK)
    window = s[start:pos - max(0, off) + len(t) + SLACK]
    blocks = [b for b in difflib.SequenceMatcher(None, t, window, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return None
    matched = sum(b.size for b in blocks)
    s0, s1 = blocks[0].b, blocks[-1].b + blocks[-1].size
    span = s1 - s0
    return matched / len(t), matched / span if span else 0.0, start + s0, start + s1


class PassageIndex:
    def __init__(self, db, pack):
        self.db, self.pack = db, pack
        self._pack_con: sqlite3.Connection | None = None
        self._pack_rows: list[tuple[str, str, str]] = []     # rowid-1 -> (키, 번역문, 분류)
        self._pack_stamp = None
        self._cache: dict[str, Cover | None] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 후보
    def warm(self) -> None:
        """번역 묶음 메모리 색인을 미리 만든다 (3만 줄에 약 1초). 첫 판정이 그만큼 기다리지 않게 뒤에서 부른다."""
        self._pack_index()

    def _pack_index(self) -> None:
        with self._lock:
            self._pack_index_locked()

    def _pack_index_locked(self) -> None:
        stamp = id(self.pack._table)
        if self._pack_con is not None and stamp == self._pack_stamp:
            return
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.execute("CREATE VIRTUAL TABLE pfts USING fts5(k, tokenize='trigram', detail='none')")
        rows = [(k, ko, cat) for k, (ko, cat) in self.pack._table.items() if len(k) >= MIN_KEY and "{" not in ko]
        con.executemany("INSERT INTO pfts(rowid, k) VALUES (?, ?)", [(i + 1, r[0]) for i, r in enumerate(rows)])
        con.commit()
        self._pack_con, self._pack_rows, self._pack_stamp = con, rows, stamp

    def _candidates(self, key: str) -> list[tuple[str, str, str, str]]:
        """(키, 원문, 한국어, via). 닻이 든 글. 먼저 닻을 전부 가진 글을, 없으면 닻 일부만 가진 글을 닻이 많이 든 순으로."""
        anchors = _anchors(key)
        lo, hi = int(len(key) * 0.6), MAX_KEY
        for loose in (False, True):
            q = _fts_query(anchors, loose)
            limit = LIMIT_LOOSE if loose else LIMIT
            out = []
            try:
                for k, zh, ko, src in self.db._db().execute(
                        "SELECT t.k, t.zh, t.ko, t.src FROM fts JOIN texts t ON t.id = fts.rowid "
                        "WHERE fts MATCH ? AND t.ko IS NOT NULL AND length(t.k) BETWEEN ? AND ? LIMIT ?", (q, lo, hi, limit)):
                    if "{" not in ko:
                        out.append((k, zh, ko, "official"))
            except sqlite3.Error:
                pass
            try:
                self._pack_index()
                for (rid,) in self._pack_con.execute("SELECT rowid FROM pfts WHERE pfts MATCH ? LIMIT ?", (q, limit)):
                    k, ko, _cat = self._pack_rows[rid - 1]
                    if lo <= len(k) <= hi:
                        out.append((k, k, ko, "pack"))
            except sqlite3.Error:
                pass
            if out:
                if loose and len(out) > KEEP:
                    out.sort(key=lambda r: -sum(a in r[0] for a in anchors))
                    out = out[:KEEP]
                return out
        return []

    # ------------------------------------------------------------ 맞추기
    def _best(self, key: str, cover_min: float, scope_min: float = 0.0) -> _Cand | None:
        anchors = _anchors(key)
        best: _Cand | None = None
        for k, zh, ko, via in self._candidates(key):
            got = align(key, k, anchors)
            if got is None:
                continue
            cover, ident, s0, s1 = got
            # 일치도는 덮은 구간 안에 원문 쪽 글자가 끼어 있으면 낮아진다 (T 가 통째로 덮였는데 0.82 로 버려진 사례가 있었다)
            if cover < cover_min or ident < (IDENT_MIN if cover < 0.95 else IDENT_LOOSE):
                continue
            if scope_min and len(key) / len(k) < scope_min:
                continue
            cand = _Cand(k, zh, ko, via, cover, ident, s0, s1)
            # 더 많이 덮은 것, 같으면 더 작은 글
            if best is None or (round(cand.cover, 2), -len(k)) > (round(best.cover, 2), -len(best.key)):
                best = cand
        return best

    def _ko_for(self, c: _Cand, allow_scope: bool = True) -> str | None:
        """맞은 구간에 해당하는 한국어. 구간이 글 전체에 가까우면 통째로, 아니면 문장 단위로 자른다.
        allow_scope=False 면 '구간이 글의 일부인데 문장도 못 자르는' 경우(통째 번역을 대신 보여야 하는 경우)는 None."""
        whole = (c.s1 - c.s0) / len(c.key)
        if whole >= 0.9:
            return tn.display_ko(c.ko)
        if c.via == "official":
            zs, ks = zh_sentences(c.zh), ko_sentences(c.ko)
            if len(zs) == len(ks) >= 2 and sum(len(tn.key(s)) for s in zs) == len(c.key):
                pos, first, last = 0, None, None
                for i, s in enumerate(zs):
                    n = len(tn.key(s))
                    if first is None and pos + n > c.s0:
                        first = i
                    if pos < c.s1:
                        last = i
                    pos += n
                if first is not None and last is not None:
                    got = " ".join(ks[first:last + 1])
                    if _plausible(got, ks, [len(tn.key(s)) for s in zs], first, last):
                        return tn.display_ko(got)
            if len(zs) >= 2 and len(ks) >= 2 and sum(len(tn.key(s)) for s in zs) == len(c.key):
                got = self._slice_aligned(c, zs, ks)
                if got:
                    return got
        return tn.display_ko(c.ko) if allow_scope and whole >= SCOPE_MIN else None

    def _slice_aligned(self, c: _Cand, zs: list[str], ks: list[str]) -> str | None:
        """중·한 문장 수가 다른 글: 길이로 문장을 짝지어(align_sentences) 화면에 보이는 구간의 한국어 문장만 뽑는다."""
        zl = [len(tn.key(s)) for s in zs]
        pairs = align_sentences(zl, [len(re.sub(r"\s", "", s)) for s in ks], _quote_flags(zs), _quote_flags(ks))
        if not pairs:
            return None
        starts = [0]
        for n in zl:
            starts.append(starts[-1] + n)
        first = next((i for i in range(len(zs)) if starts[i + 1] > c.s0), None)
        last = max((i for i in range(len(zs)) if starts[i] < c.s1), default=None)
        if first is None or last is None or last < first:
            return None
        k0 = k1 = None
        for z0, z1, j0, j1 in pairs:
            if z1 <= first or z0 > last:
                continue
            if j1 > j0:
                k0 = j0 if k0 is None else min(k0, j0)
                k1 = j1 if k1 is None else max(k1, j1)
        if k0 is None or k1 is None:
            return None
        text = " ".join(ks[k0:k1])
        if not _plausible(text, ks, zl, first, last):
            return None
        return tn.display_ko(text)

    # ------------------------------------------------------------ 바깥에서 부르는 곳
    def cover(self, text: str) -> Cover | None:
        key = tn.key(text)
        if len(key) < MIN_KEY or len(key) > MAX_KEY or tn.cjk_count(key) < len(key) * 0.7:
            return None
        if key in self._cache:
            return self._cache[key]
        found = self._cover(text, key)
        if len(self._cache) >= CACHE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = found
        return found

    def _cover(self, text: str, key: str) -> Cover | None:
        c = self._best(key, COVER_MIN)
        if c is not None:
            ko = self._ko_for(c, allow_scope=False)         # 통째로 맞거나 문장 단위로 잘리는 경우
            if ko:
                return Cover(ko, c.zh, c.via, c.cover, c.ident)
        # 한 글로 안 덮이거나 큰 글의 일부일 뿐이면: 공식 항목 여러 개가 이어 읽힌 것일 수 있다. 문장 2~5개씩 묶어 하나씩 맞춘다
        multi = self._cover_entries(text)
        if multi is not None:
            return multi
        if c is not None:
            ko = self._ko_for(c)                            # 못 자르면 통째로 (구간이 글의 SCOPE_MIN 이상일 때만)
            if ko:
                return Cover(ko, c.zh, c.via, c.cover, c.ident)
        return None

    def _cover_entries(self, text: str) -> Cover | None:
        sents = [s for s in _SENT.findall(text) if tn.key(s)]
        if len(sents) < 2:
            return None
        out: list[str] = []
        cover_sum = ident_sum = 0.0
        i = 0
        while i < len(sents):
            for span in range(min(SENT_SPAN, len(sents) - i), 0, -1):
                piece = tn.key("".join(sents[i:i + span]))
                if len(piece) < 8:
                    continue
                cand = self._best(piece, STRICT_COVER, scope_min=0.7)
                if cand is not None:
                    out.append(tn.display_ko(cand.ko))
                    cover_sum += cand.cover
                    ident_sum += cand.ident
                    i += span
                    break
            else:
                if i == 0 and len(tn.key(sents[0])) <= 12 and len(sents) > 1:
                    out.append(sents[0].strip())     # 모듈 스토리 맨 앞 제목 한 줄은 공식 항목이 아니다
                    i += 1
                else:
                    return None
        n = max(1, len(out))
        return Cover(" ".join(out), text, "official", cover_sum / n, ident_sum / n, parts=len(out))
