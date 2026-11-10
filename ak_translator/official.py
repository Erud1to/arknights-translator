"""공식 번역 DB 조회.

찾는 순서 (빠른 것부터)
  1. 지금 읽고 있는 스토리의 근처 줄 — 다음 대사는 거의 항상 바로 다음 줄이다
  2. 전체 문장 정확 일치
  3. 숫자가 바뀌는 문장 틀 (스킬 설명 등)
  4. 비슷한 문장 찾기 — OCR 이 글자 몇 개를 틀렸을 때
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass

from . import textnorm as tn

MIN_FUZZY_LEN = 6
FUZZY_RATIO = 0.84
# 긴 글은 우연히 이만큼 닮기 어렵다. 기울인 글씨 설명문은 58자 중 11자를 잘못 읽어 0.79 였다.
# 2등과 차이가 클 때만 낮춘 기준을 쓴다 (숫자만 다른 비슷한 문장끼리 헷갈리지 않게)
FTS_MIN = 5          # 세 글자 색인(fts)에 넣은 키의 최소 길이 (build_db 와 같다)
COMMON_PHRASE = 3   # 통합전략 밖의 게임 글에 이만큼 나오면 이름이 아니라 보통 말로 본다 (rogue_names)
# 고유 이름만 든 표 (cn_names). 한국 서버에 없는 이름은 번역 묶음의 번역을 용어로 쓴다.
NAME_SOURCES = ("excel:skill_table", "excel:item_table", "excel:uniequip_table", "excel:stage_table",
                "excel:zone_table", "excel:gacha_table", "excel:skin_table", "excel:retro_table")
# 통합전략 표는 유물·노드 이름과 함께 '行动力耗尽后' 같은 짧은 문구도 섞여 있어 걸러서 쓴다.
# 안 쓰면 유물 카드 제목은 '견고한 방어의 손', 구매 확인 '是否消耗16购买坚御之手？' 의 기계 번역은 '견고한 손' 이었다
ROGUE_SOURCE = "excel:roguelike_topic_table"
# 문구 끝·첫머리 ('…耗尽后', '坏天气生成', '步数发放', '完成遭遇战'). 이런 시스템 문구는 이름이 아니다
_PHRASE_ZH = re.compile(r"(?:[后时中的了着过吗呢吧啊]|解锁|生成|发放|获取|提升|降低|增加|减少|完成)$"
                        r"|^(?:完成|获得|解锁|通过|击败|消耗|进入|选择)")
_CLAUSE_KO = re.compile(r"(?:다|요|까|고|며|면|서|후|때|중|기|음)\s*$")   # 번역이 절·문장이면 이름이 아니다
ROGUE_NAME_KO_MAX = 14
# 한국 서버에 없는 틀을 번역할 차례 (untranslated_templates)
TEMPLATE_ORDER = ("excel:skill_table", "excel:character_table", "excel:uniequip_table", "excel:building_data",
                  "excel:roguelike_topic_table", "excel:activity_table")
LONG_FUZZY_LEN = 30
LONG_FUZZY_RATIO = 0.74
FUZZY_MARGIN = 0.12
FUZZY_FETCH = 1000   # 비슷한 문장 후보를 이만큼까지 받아
FUZZY_KEEP = 40      # 겹치는 조각이 많은 이만큼만 자세히 견준다
# 미리 번역 묶음 표가 없는 예전 DB 에서 표 이름으로 나누는 법
OLD_TAGS = {
    "excel:roguelike_topic_table": "rogue", "excel:roguelike_table": "rogue",
    "excel:sandbox_table": "sandbox", "excel:sandbox_perm_table": "sandbox",
    "excel:handbook_info_table": "char:", "excel:charword_table": "char:", "excel:uniequip_table": "char:",
    "excel:enemy_handbook_table": "enemy",
}
CONTEXT_RATIO = 0.72
LOOKAHEAD = 12
LOOKBEHIND = 4
LOCK_LEN = 8     # 이 정도 길이는 되어야 "이 스토리를 읽는 중"이라고 판단한다
_COUNTDOWN = re.compile(r"(?:\d+|\{\d+\})\s*(?:天|小时|分钟|秒)")   # 남은 기간 '12天'
_NAME_ZH = re.compile(r"[㐀-鿿·]+")                        # 한자(와 가운뎃점)만으로 된 글
_SENTENCE_KO = re.compile(r"(?:다|요|까|[.!?…])\s*$")       # 번역이 문장이면 이름이 아니라 선택지·설명이다


@dataclass
class Hit:
    zh: str
    ko: str | None        # None 이면 원문은 확인됐지만 공식 번역이 없다
    via: str              # story / exact / template / fuzzy
    score: float = 1.0
    file_id: int = -1
    idx: int = -1
    src: str = ""         # 이 글이 나온 표 (story·speaker 는 대사)


def _all_grams(k: str) -> str:
    """k 가 든 글을 찾는 fts 질의. 색인이 detail=none 이라 구절 검색이 안 되어 세 글자 조각을 모두 AND 로 건다
    (조각 순서까지는 못 보므로 받은 뒤 'k in 키' 로 한 번 더 거른다)."""
    grams = dict.fromkeys(k[i:i + 3].replace('"', "") for i in range(len(k) - 2))
    return " AND ".join(f'"{g}"' for g in grams if g)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = sorted((len(a), len(b)))
    if shorter / longer < 0.6:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


class OfficialDB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        self.file_id = -1
        self.idx = -1
        self._lines: list[tuple] = []     # 현재 스토리의 (idx, k, zh, ko, kind)
        self._short: dict | None = None   # _short_ui
        self._short_lock = threading.Lock()
        db = self._db()
        self.meta = dict(db.execute("SELECT name, value FROM meta"))

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._local.db = db
        return db

    # ------------------------------------------------------------------ 조회
    def lookup(self, text: str, fuzzy: bool = True) -> Hit | None:
        k = tn.key(text)
        if not k:
            return None
        hit = self._in_context(k) or self._exact(k)
        if hit:
            return hit
        tpl = tn.template_key(text)
        # 글자가 한둘뿐인 틀은 아이콘을 잘못 읽은 조각과도 맞아 버린다 ('对5' → '5페어.').
        # 다만 '12天' 같은 숫자+시간 단위는 터미널·이벤트의 남은 기간에 늘 나오므로 틀도 꼭 그 모양일 때만 쓴다
        countdown = bool(_COUNTDOWN.fullmatch(text.strip()))
        if tn.SLOT in tpl and (tn.cjk_count(tpl) >= 3 or countdown):
            hit = self._template(tpl, text)
            if hit and (tn.cjk_count(tpl) >= 3 or _COUNTDOWN.fullmatch(hit.zh.strip())):
                return hit
        if fuzzy:
            return self._fuzzy(k)
        return None

    def term(self, zh: str) -> str | None:
        row = self._db().execute("SELECT ko FROM terms WHERE zh=?", (zh,)).fetchone()
        return row[0] if row else None

    def _exact(self, k: str) -> Hit | None:
        row = self._db().execute("SELECT zh, ko, src FROM texts WHERE k=?", (k,)).fetchone()
        if not row:
            return None
        hit = Hit(zh=row[0], ko=row[1], via="exact", src=row[2])
        if len(k) >= LOCK_LEN:
            self._try_lock(k, hit)
        return hit

    def _template(self, tpl: str, text: str) -> Hit | None:
        row = self._db().execute("SELECT zh, ko, src FROM templates WHERE k=?", (tpl,)).fetchone()
        if not row:
            return None
        if row[1] is None:
            # 한국 서버에 없는 틀: 번역은 번역 묶음에 있다 (resolver._pack_template 이 화면 숫자로 채운다)
            return Hit(zh=row[0], ko=None, via="template", src=row[2])
        filled = tn.fill_template(row[0], row[1], text)
        if filled is None:
            return None
        return Hit(zh=row[0], ko=filled, via="template", src=row[2])

    def templates_containing(self, tk: str, min_ratio: float, limit: int = 40) -> list[tuple[str, str | None]]:
        """(원문 틀, 번역 틀 또는 None). 숫자를 #으로 바꾼 틀 키 안에 tk 가 통째로 들고 tk 가 그 틀의 min_ratio 이상인 틀.
        줄이 잘려 일부만 읽힌 스킬 설명을 틀에서 찾는 데 쓴다."""
        if len(tk) < 8:
            return []
        try:
            rows = self._db().execute(
                "SELECT zh, ko FROM templates WHERE instr(k, ?) > 0 AND length(k) > ? AND length(k) <= ? LIMIT ?",
                (tk, len(tk), int(len(tk) / min_ratio), limit)).fetchall()
        except sqlite3.Error:
            return []
        return rows

    def _fuzzy(self, k: str) -> Hit | None:
        if len(k) < MIN_FUZZY_LEN:
            return None
        # 글자 하나를 틀리면 그 글자를 포함한 세 조각만 어긋난다. 나머지 조각으로 후보를 찾는다
        grams = {k[i:i + 3] for i in range(len(k) - 2)}
        grams = sorted(g for g in grams if '"' not in g)
        if len(grams) > 24:
            step = len(grams) / 24
            grams = [grams[int(i * step)] for i in range(24)]
        query = " OR ".join(f'"{g}"' for g in grams)
        try:
            # 길이가 크게 다른 문장을 먼저 걸러야 흔한 조각에 후보 자리를 뺏기지 않는다.
            # 색인이 detail=none 이라 rank 가 순서를 못 매긴다 (번호순으로 나온다). 넉넉히 받아 겹치는
            # 조각 수로 직접 고른다. 안 그러면 긴 문단은 흔한 조각('我知道')에 걸린 앞 번호들에 밀린다
            rows = self._db().execute(
                "SELECT t.k, t.zh, t.ko, t.src FROM fts JOIN texts t ON t.id = fts.rowid "
                "WHERE fts MATCH ? AND length(t.k) BETWEEN ? AND ? LIMIT ?",
                (query, int(len(k) * 0.75), int(len(k) * 1.35) + 1, FUZZY_FETCH),
            ).fetchall()
        except sqlite3.Error:
            return None
        mine = {k[i:i + 3] for i in range(len(k) - 2)}
        if len(rows) > FUZZY_KEEP:
            rows.sort(key=lambda r: -len(mine.intersection(r[0][i:i + 3] for i in range(len(r[0]) - 2))))
        best, best_score, second = None, 0.0, 0.0
        for cand_k, zh, ko, src in rows[:FUZZY_KEEP]:
            score = similarity(k, cand_k)
            if score > best_score:
                best, best_score, second = (cand_k, zh, ko, src), score, best_score
            elif score > second:
                second = score
        long_ok = len(k) >= LONG_FUZZY_LEN and best_score >= LONG_FUZZY_RATIO and best_score - second >= FUZZY_MARGIN
        if best and (best_score >= FUZZY_RATIO or long_ok):
            hit = Hit(zh=best[1], ko=best[2], via="fuzzy", score=best_score, src=best[3])
            if len(best[0]) >= LOCK_LEN:
                self._try_lock(best[0], hit)
            return hit
        return None

    # ------------------------------------------------------------- 스토리 위치
    def _in_context(self, k: str) -> Hit | None:
        with self._lock:
            lines = self._lines
            pos = self.idx
        if not lines:
            return None
        lo = max(0, pos - LOOKBEHIND)
        hi = min(len(lines), pos + LOOKAHEAD + 1)
        best, best_score = None, 0.0
        for i in range(lo, hi):
            line = lines[i]
            score = 1.0 if line[1] == k else similarity(k, line[1])
            # 같은 점수면 앞으로 나아가는 쪽을 고른다
            if score > best_score or (score == best_score and i > pos and best is not None and best[0] <= pos):
                best, best_score = (i, line), score
        if best is None:
            return None
        need = 1.0 if len(k) < 4 else CONTEXT_RATIO
        if best_score < need:
            return None
        i, line = best
        with self._lock:
            self.idx = i
        return Hit(zh=line[2], ko=line[3], via="story", score=best_score,
                   file_id=self.file_id, idx=i)

    def _try_lock(self, k: str, hit: Hit) -> None:
        """이 문장이 스토리에 있으면 그 파일을 '지금 읽는 중'으로 잡는다."""
        rows = self._db().execute(
            "SELECT s.file_id, s.idx FROM story s JOIN files f ON f.id = s.file_id "
            "WHERE s.k=? AND f.path NOT LIKE '[uc]info%' LIMIT 8",
            (k,),
        ).fetchall()
        # 이벤트 대사가 메인 스토리에 그대로 재사용되는 일이 있다(32만 줄 중 1,220개). 어느 쪽을
        # 잡아도 문장은 같고, 틀렸으면 다음 대사에서 다시 잡힌다. 여러 곳이면 흔한 말이니 버린다
        if not rows or len({r[0] for r in rows}) > 3:
            return
        file_id, idx = rows[0]
        lines = self._db().execute(
            "SELECT idx, k, zh, ko, kind FROM story WHERE file_id=? ORDER BY idx", (file_id,)
        ).fetchall()
        with self._lock:
            self.file_id, self.idx, self._lines = file_id, idx, lines
        hit.file_id, hit.idx = file_id, idx

    def current_line(self) -> tuple | None:
        with self._lock:
            lines, pos = self._lines, self.idx
        return lines[pos] if lines and 0 <= pos < len(lines) else None

    def story_info(self) -> tuple[int, str, str]:
        """지금 맞춘 스토리 줄의 (파일 번호, 화자 원문, 화자 공식 한국어). 스토리 기록 탭에 쓴다."""
        with self._lock:
            file_id, idx = self.file_id, self.idx
        if file_id < 0:
            return -1, "", ""
        row = self._db().execute("SELECT speaker_zh, speaker_ko FROM story WHERE file_id=? AND idx=?",
                                 (file_id, idx)).fetchone()
        return file_id, (row[0] or "") if row else "", (row[1] or "") if row else ""

    def story_title(self, file_id: int) -> str:
        """스토리 파일의 이름: 이벤트·오퍼레이터 이름(공식 한국어) · 파일 이름 ('level_a001_01_beg')."""
        base = os.path.splitext(os.path.basename(self.file_path(file_id)))[0]
        row = self._db().execute("SELECT g.name FROM story_tags s JOIN groups g ON g.tag = s.tag "
                                 "WHERE s.file_id=?", (file_id,)).fetchone()
        return f"{row[0]} · {base}" if row and row[0] else base

    def next_line(self) -> tuple | None:
        """지금 위치 바로 다음 줄 (idx, k, zh, ko, kind). 스토리를 따라가는 중이 아니면 None."""
        with self._lock:
            lines, pos = self._lines, self.idx
        if not lines or pos < 0 or pos + 1 >= len(lines):
            return None
        return lines[pos + 1]

    def position(self) -> tuple[str, int]:
        """(스토리 파일 경로, 줄 번호). 문제 화면 기록용."""
        if self.file_id < 0:
            return "", -1
        row = self._db().execute("SELECT path FROM files WHERE id=?", (self.file_id,)).fetchone()
        return (row[0] if row else ""), self.idx

    # ------------------------------------------------------------ Gemini 용 자료
    def script(self, file_id: int) -> list[tuple]:
        """(idx, kind, 화자, 화자 한국 이름, 원문, 공식 번역) 를 순서대로."""
        return self._db().execute(
            "SELECT idx, kind, speaker_zh, speaker_ko, zh, ko FROM story WHERE file_id=? ORDER BY idx",
            (file_id,),
        ).fetchall()

    def file_path(self, file_id: int) -> str:
        row = self._db().execute("SELECT path FROM files WHERE id=?", (file_id,)).fetchone()
        return row[0] if row else ""

    def speaker_examples(self, speakers: set[str], count: int) -> dict[str, tuple[str, list]]:
        """{화자: (공식 한국 이름, 그 인물의 한국 서버 대사 몇 줄)}. 화자 여럿을 한 번에 찾는다.

        '？？？' 처럼 정체를 숨긴 화자는 스토리마다 다른 인물이라 예시가 오히려 말투를 흐린다. 뺀다.
        """
        names = [s for s in speakers if tn.has_cjk(s)]
        out: dict[str, tuple[str, list]] = {s: ("", []) for s in speakers}
        if not names:
            return out
        marks = ",".join("?" * len(names))
        rows = self._db().execute(
            f"SELECT speaker_zh, speaker_ko, zh, ko, file_id FROM story WHERE speaker_zh IN ({marks}) "
            "AND ko IS NOT NULL AND kind='say' AND length(zh) BETWEEN 8 AND 60",
            names,
        ).fetchall()
        by_speaker: dict[str, list] = {}
        for row in rows:
            by_speaker.setdefault(row[0], []).append(row)
        for speaker, found in by_speaker.items():
            counts: dict[str, int] = {}
            for row in found:
                counts[row[1]] = counts.get(row[1], 0) + 1
            name = max(counts, key=counts.get) if counts else ""
            # 최근 파일의 대사가 지금 말투에 가깝다. 같은 파일에서 몰리지 않게 파일마다 하나씩 고른다
            picked, seen = [], set()
            for _s, _sk, zh, ko, file_id in sorted(found, key=lambda r: -r[4]):
                if file_id in seen:
                    continue
                seen.add(file_id)
                picked.append((tn.strip_rich(zh), tn.strip_rich(ko)))
                if len(picked) >= count:
                    break
            out[speaker] = (name or "", picked)
        return out

    def narration_examples(self, count: int) -> list[tuple[str, str]]:
        rows = self._db().execute(
            "SELECT zh, ko FROM story WHERE speaker_zh='' AND kind='say' AND ko IS NOT NULL "
            "AND length(zh) BETWEEN 12 AND 40 AND file_id % 97 = 3 LIMIT ?",
            (count,),
        ).fetchall()
        return [(tn.strip_rich(z), tn.strip_rich(k)) for z, k in rows]

    def upcoming(self, count: int) -> list[str]:
        """지금 위치 다음에 나올, 공식 번역이 없는 문장들 (미리 번역해 두려고)."""
        with self._lock:
            lines, pos = self._lines, self.idx
        out = []
        for line in lines[pos + 1:pos + 1 + count]:
            if line[3] is None and tn.has_cjk(line[2]):
                out.append(_clean_zh(line[2]))
        return out

    def story_speakers(self) -> set[str]:
        if self.file_id < 0:
            return set()
        rows = self._db().execute(
            "SELECT DISTINCT speaker_zh FROM story WHERE file_id=? AND speaker_zh != ''",
            (self.file_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def all_terms(self) -> list[tuple[str, str, str, int]]:
        return self._db().execute("SELECT zh, ko, kind, n FROM terms").fetchall()

    # ------------------------------------------------------------ 미리 번역 자료
    def untranslated_story(self) -> list[tuple[int, str, str]]:
        """(파일 번호, 파일 경로, 원문). 공식 번역이 없는 스토리 줄을 파일·줄 순서대로."""
        return self._db().execute(
            "SELECT s.file_id, f.path, s.zh FROM story s JOIN files f ON f.id = s.file_id "
            "WHERE s.ko IS NULL ORDER BY s.file_id, s.idx"
        ).fetchall()

    def story_tags(self) -> dict[int, str]:
        """{파일 번호: 미리 번역 묶음 ('act:act55side', 'char:char_002_amiya', 'rogue')}.
        묶음 표가 생기기 전에 만든 DB 면 빈 값 (그러면 경로로 나눈다)."""
        try:
            return dict(self._db().execute("SELECT file_id, tag FROM story_tags").fetchall())
        except sqlite3.Error:
            return {}

    def group_info(self) -> dict[str, tuple[str, int, int]]:
        """{묶음: (보일 이름, 정렬값, 한국 서버에 아직 없는가)}."""
        try:
            rows = self._db().execute("SELECT tag, name, sort, fresh FROM groups").fetchall()
        except sqlite3.Error:
            return {}
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def untranslated_tagged(self) -> list[tuple[str, str]]:
        """(원문, 묶음). 표에서 온 글 가운데 공식 번역이 없는 것. 여러 줄짜리는 줄마다 따로 들어 있으니 뺀다."""
        try:
            rows = self._db().execute(
                "SELECT t.zh, g.tag FROM tags g JOIN texts t ON t.k = g.k WHERE t.ko IS NULL"
            ).fetchall()
        except sqlite3.Error:
            # 묶음 표가 생기기 전 DB: 표 이름으로만 나눈다 (오퍼레이터는 한 묶음)
            marks = ",".join("?" * len(OLD_TAGS))
            rows = [(zh, OLD_TAGS[src]) for zh, src in self._db().execute(
                f"SELECT zh, src FROM texts WHERE ko IS NULL AND src IN ({marks})", tuple(OLD_TAGS))]
        return [(zh, tag) for zh, tag in rows if "\n" not in zh and "\\n" not in zh]

    def file_speakers(self, file_id: int) -> set[str]:
        rows = self._db().execute(
            "SELECT DISTINCT speaker_zh FROM story WHERE file_id=? AND speaker_zh != ''", (file_id,)
        ).fetchall()
        return {r[0] for r in rows}

    def char_names(self) -> list[tuple[str, str]] | None:
        """(중국어 이름, 한국 서버 이름 또는 ''). 캐릭터 표가 생기기 전에 만든 DB 면 None."""
        try:
            return self._db().execute("SELECT zh, ko FROM chars").fetchall()
        except sqlite3.Error:
            return None

    def recruit(self) -> tuple[list, list, dict] | None:
        """공개모집 표 (오퍼레이터 [(이름, 별, 태그 JSON)], 태그 이름들, 규칙). 표가 생기기 전에 만든 DB 면 None."""
        try:
            db = self._db()
            ops = db.execute("SELECT name, rarity, tags FROM recruit_ops").fetchall()
            tags = [r[0] for r in db.execute("SELECT name FROM recruit_tags")]
        except sqlite3.Error:
            return None
        if not ops:
            return None
        return ops, tags, json.loads(self.meta.get("recruit_rule") or "{}")

    def name_usage(self, names: dict[str, str]) -> dict[str, tuple[int, int]]:
        """이름마다 (그 이름이 든 공식 번역 글 수, 그 가운데 번역문에 한국 이름까지 든 글 수).
        두 글자 이름이 보통 말로도 쓰이는지('真理' 진리 / 이스티나) 가늠한다. 글 전체를 한 번 훑는다 (몇 초)."""
        sizes = {len(zh) for zh in names}
        seen = dict.fromkeys(names, 0)
        named = dict.fromkeys(names, 0)
        for zh, ko in self._db().execute("SELECT zh, ko FROM texts WHERE ko IS NOT NULL AND ko != ''"):
            for name in {zh[i:i + s] for s in sizes for i in range(len(zh) - s + 1)} & names.keys():
                seen[name] += 1
                if names[name] in ko:
                    named[name] += 1
        return {name: (seen[name], named[name]) for name in names}

    def untranslated_untagged(self) -> list[str]:
        """표에서 온 글 가운데 공식 번역도 없고 미리 번역 묶음(이벤트·오퍼레이터…)에도 안 든 것 (메달·상점 등)."""
        try:
            rows = self._db().execute(
                "SELECT zh FROM texts WHERE ko IS NULL AND src NOT IN ('story', 'speaker') "
                "AND k NOT IN (SELECT k FROM tags)"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [r[0] for r in rows if "\n" not in r[0] and "\\n" not in r[0]]

    def untranslated_templates(self) -> list[tuple[str, str]]:
        """(틀 원문, 표). 숫자가 바뀌는 글 가운데 한국 서버에 없는 것 (스킬·재능·모듈·기반시설·유물 설명 등).
        자주 보는 것부터: 스킬 → 오퍼레이터(재능·특성) → 모듈 → 기반시설 → 나머지."""
        try:
            rows = self._db().execute("SELECT zh, src FROM templates WHERE ko IS NULL").fetchall()
        except sqlite3.Error:
            return []
        first = {s: i for i, s in enumerate(TEMPLATE_ORDER)}
        rows.sort(key=lambda r: (first.get(r[1], len(first)), r[1] or ""))
        # 자리표시자가 있는 틀만. 숫자만 든 글('剩余15')은 글 그대로 묶음의 다른 분류에 있고, 스토리는 story_pack 이 맡는다
        return [(zh, src) for zh, src in rows
                if "{" in zh and src not in ("story", "speaker") and tn.cjk_count(zh) >= 2]

    def rogue_names(self) -> list[tuple[str, str]]:
        """통합전략의 유물·이벤트·노드 이름 (중국어, 공식 한국어). 용어 표에 없어 기계 번역이 '林间夜话' 를
        '숲속의 밤 이야기' 로 옮겼다. 한자만으로 된 3~8자 글 가운데 번역이 문장(…다, …요)이 아닌 것만 이름으로 본다.
        다른 게임 글(스토리 등)에도 세 번 넘게 나오면 보통 말이라 뺀다: 유물 이름 '危机四伏'(사면초가),
        '没有回答'(무응답)을 지키면 '대답하지 않았다' 가 '무응답' 이 된다. 모두 1.3초쯤 걸린다 (뒤 스레드에서 부른다)."""
        db = self._db()
        try:
            rows = db.execute(
                "SELECT zh, ko FROM texts WHERE src = 'excel:roguelike_topic_table' AND ko IS NOT NULL "
                "AND length(k) BETWEEN 3 AND 8").fetchall()
        except sqlite3.Error:
            return []
        out = []
        for zh, ko in rows:
            if not _NAME_ZH.fullmatch(zh) or _SENTENCE_KO.search(ko) or "<" in ko or "{" in ko:
                continue
            elsewhere = db.execute(
                "SELECT t.k FROM fts JOIN texts t ON t.id = fts.rowid WHERE fts MATCH ? "
                "AND t.src NOT LIKE '%roguelike%' LIMIT 20", (_all_grams(zh),)).fetchall()
            if sum(zh in k for (k,) in elsewhere) < COMMON_PHRASE:
                out.append((zh, ko))
        return out

    def cn_names(self, ko_of) -> list[tuple[str, str]]:
        """한국 서버에 아직 없는 이름 (스킬·아이템·모듈·스테이지·헤드헌팅 등) 과 번역 묶음의 번역. ko_of(키) → 번역 또는 None.
        용어로 지키지 않으면 같은 스킬 이름이 묶음에서는 '코로나의 격렬한 활동', 그 이름이 든 알림 문장
        '技能日冕重烈已完成专精等级2' 의 기계 번역에서는 '코로나 재점화' 로 따로 놀았다. rogue_names 처럼 다른 글에
        세 번 넘게 나오는 흔한 말은 뺀다."""
        db = self._db()
        sources = NAME_SOURCES + (ROGUE_SOURCE,)
        marks = ",".join("?" * len(sources))
        try:
            rows = db.execute(
                f"SELECT k, zh, src FROM texts WHERE (ko IS NULL OR ko = '') AND src IN ({marks}) "
                "AND length(k) BETWEEN 3 AND 8", sources).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for k, zh, src in rows:
            ko = ko_of(k)
            if not ko or not _NAME_ZH.fullmatch(zh) or _SENTENCE_KO.search(ko) or "<" in ko or "{" in ko:
                continue
            if src == ROGUE_SOURCE and (_PHRASE_ZH.search(zh) or _CLAUSE_KO.search(ko)
                                        or len(ko) > ROGUE_NAME_KO_MAX):
                continue
            elsewhere = db.execute(
                f"SELECT t.k FROM fts JOIN texts t ON t.id = fts.rowid WHERE fts MATCH ? "
                f"AND t.src NOT IN ({marks}) LIMIT 20", (_all_grams(zh), *sources)).fetchall()
            if sum(zh in ck for (ck,) in elsewhere) < COMMON_PHRASE:
                out.append((zh, ko))
        return out

    def completions(self, k: str, extra: int) -> list[tuple[str, str, str]]:
        """(키, 원문, 공식 번역). 키 k 앞뒤에 모두 extra 글자를 더한 화면 글(대사 제외).
        세 글자 색인(fts)으로 k 가 든 글만 받아 오므로 빠르다."""
        if len(k) < 3:
            return []
        if len(k) + extra < FTS_MIN:
            return [(ck, zh, ko) for ck, zh, ko in self._short_ui().get(k, []) if len(ck) == len(k) + extra]
        try:
            rows = self._db().execute(
                "SELECT t.k, t.zh, t.ko, t.src FROM fts JOIN texts t ON t.id = fts.rowid "
                "WHERE fts MATCH ? AND length(t.k) = ? AND t.ko IS NOT NULL LIMIT 50",
                (_all_grams(k), len(k) + extra),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [(ck, zh, ko) for ck, zh, ko, src in rows if k in ck and src not in ("story", "speaker")]

    def containing(self, k: str, min_ratio: float, limit: int = 40) -> list[tuple[str, str, str, str]]:
        """(키, 원문, 공식 번역, 표). 키 k 가 통째로 들어 있고 k 가 그 글의 min_ratio 이상인 글.
        문단이 화면 가장자리에서 잘려 앞이나 뒤가 빠진 채 읽힌 글을 찾는 데 쓴다. 대사도 포함한다 (k 가 열두 글자 넘게 길다)."""
        if len(k) < FTS_MIN:
            return []
        try:
            rows = self._db().execute(
                "SELECT t.k, t.zh, t.ko, t.src FROM fts JOIN texts t ON t.id = fts.rowid "
                "WHERE fts MATCH ? AND t.ko IS NOT NULL AND length(t.k) > ? AND length(t.k) <= ? LIMIT ?",
                (_all_grams(k), len(k), int(len(k) / min_ratio), limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [row for row in rows if k in row[0]]

    def _short_ui(self) -> dict[str, list[tuple[str, str, str]]]:
        """색인(fts)에 없는 네 글자 화면 글을 앞이나 뒤 한 글자를 뗀 키로 찾는 표 ('示我以' → '示我以真').
        fts 는 다섯 글자 넘는 키만 담고, 앞에 와일드카드가 붙은 GLOB 은 0.5초 걸려 처음 한 번만 표로 만든다."""
        with self._short_lock:
            if self._short is None:
                table: dict[str, list] = {}
                rows = self._db().execute(
                    "SELECT k, zh, ko, src FROM texts WHERE length(k) = ? AND ko IS NOT NULL", (FTS_MIN - 1,))
                for ck, zh, ko, src in rows:
                    if src in ("story", "speaker"):
                        continue
                    for part in {ck[1:], ck[:-1]}:
                        table.setdefault(part, []).append((ck, zh, ko))
                self._short = table
            return self._short

    def glob_keys(self, pattern: str, limit: int = 3) -> list[tuple[str, str]]:
        """(키, 원문). 키가 GLOB 꼴('一次性??背包')에 맞는 글. 앞부분이 정해져 있으면 색인을 탄다."""
        return self._db().execute("SELECT k, zh FROM texts WHERE k GLOB ? LIMIT ?", (pattern, limit)).fetchall()

    def text_ko(self, zh: str) -> tuple[bool, str | None]:
        """(게임 데이터에 있는 글자인가, 공식 번역)."""
        row = self._db().execute("SELECT ko FROM texts WHERE k=?", (tn.key(zh),)).fetchone()
        return (row is not None, row[0] if row else None)


def _clean_zh(text: str) -> str:
    return re.sub(r"\{@[A-Za-z_]+\}", "博士", tn.strip_rich(text)).strip()
