"""중국 서버 / 한국 서버 게임 데이터를 맞대어 공식 번역 DB 를 만든다.

  python -I -m ... 대신 파일을 직접 실행한다 (내려받은 폴더의 코드가 import 되지 않게):
  python -I ak_translator/build_db.py --raw <저장소> --i18n <string_map 폴더> --out data/official.sqlite

입력 파일은 텍스트로만 읽는다. 실행되는 것은 없다.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import re
import sqlite3
import sys
import time

if __package__ in (None, ""):
    # -I 로 실행하면 스크립트 폴더가 sys.path 에 없으므로 프로젝트 폴더만 직접 넣는다
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ak_translator import recruit, storyparse, textnorm as tn  # noqa: E402
from ak_translator.official import FTS_MIN  # noqa: E402

# 글자가 없거나, 화면에 보이지 않는 표
SKIP_TABLES = {"audio_data", "hotupdate_meta_table", "battle_equip_table"}

# 맥락에 따라 번역이 달라지는 대사. 정해진 UI 문구와 달리 한 가지 번역을 정답으로 칠 수 없다
DIALOGUE_SOURCES = {"story", "speaker", "excel:charword_table", "excel:handbook_info_table"}
SENTENCE_END = "。！？…!?—"

NAME_FIELD = re.compile(r"(^|[a-z])(name|title)(first|second|cn)?$", re.I)
ID_FIELDS = ("id", "charId", "skillId", "itemId", "stageId", "zoneId", "skinId", "equipId",
             "uniEquipId", "enemyId", "storyId", "missionId", "groupId", "activityId")
MAX_TERM_LEN = 16


def read_json(path: str):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def usable_ko(zh: str, ko) -> bool:
    if not isinstance(ko, str) or not ko.strip():
        return False
    # 한국 서버에 아직 없는 항목은 자리표시자로 채워져 있다
    if re.search(r"UNUSED|TBD|TODO|^test|더미\s*텍스트", ko, re.I):
        return False
    if not plausible(zh, ko):
        return False
    # 번역이 덜 된 칸에는 중국어가 그대로 들어 있다
    return tn.has_hangul(ko) or not tn.has_cjk(ko)


# 한자가 이만큼 넘는 글은 번역문 길이로 짝이 맞는지 본다. 짧은 대사는 의역이 많아 보지 않는다
PLAUSIBLE_MIN_CJK = 20
# 공식 번역은 한자 1자에 한글·영숫자 1.2~2.1자다 (24만 쌍의 5~95%). 절반도 안 되면 다른 글이다
PLAUSIBLE_MIN_RATIO = 0.5
_CJK_CHAR = re.compile(r"[㐀-鿿]")
_KO_CHAR = re.compile(r"[가-힣A-Za-z0-9]")


def plausible(zh: str, ko: str) -> bool:
    """번역문 길이가 원문과 어울리는가.

    순서로 짝지은 칸은 중국 서버만 내용을 바꿨으면 엉뚱한 글과 짝지어진다. 로딩 팁의 '炎国' 설명은
    한국 서버에서 아직 같은 칸이 '용문' 설명이라, 한자 117자에 한글 50자가 붙었다.
    """
    cjk = len(_CJK_CHAR.findall(tn.strip_rich(zh)))
    if cjk < PLAUSIBLE_MIN_CJK:
        return True
    return len(_KO_CHAR.findall(tn.strip_rich(ko))) >= cjk * PLAUSIBLE_MIN_RATIO


def _consistent(cn_item, kr_item) -> bool:
    """순서로 짝지은 두 항목 안의 긴 글이 모두 서로 어울리는가."""
    return all(ko is None or plausible(zh, ko) for _path, zh, ko in walk(cn_item, kr_item, check=False))


def _id_of(item: dict):
    for name in ID_FIELDS:
        value = item.get(name)
        if isinstance(value, (str, int)):
            return name, value
    return None


def walk(cn, kr, path: tuple = (), check: bool = True):
    """같은 경로의 문자열을 짝지어 (경로, 중국어, 한국어 또는 None) 를 낸다.

    check 가 거짓이면 순서로 짝지은 항목을 따져 보지 않는다 (_consistent 가 안쪽을 다시 훑지 않게).
    """
    if isinstance(cn, str):
        if tn.has_cjk(cn):
            yield path, cn, (kr if isinstance(kr, str) else None)
    elif isinstance(cn, dict):
        other = kr if isinstance(kr, dict) else {}
        for name, value in cn.items():
            yield from walk(value, other.get(name), path + (str(name),), check)
    elif isinstance(cn, list):
        other = kr if isinstance(kr, list) else []
        by_id = {}
        if cn and all(isinstance(x, dict) and _id_of(x) for x in cn):
            field = _id_of(cn[0])[0]
            by_id = {x.get(field): x for x in other if isinstance(x, dict)}
            for item in cn:
                # 항목 ID 를 경로에 남긴다 ('act55side_m01'). 어느 이벤트·오퍼레이터의 글인지 여기서 안다
                yield from walk(item, by_id.get(item.get(field)), path + (str(item.get(field)),), check)
            return
        # 서버별로 항목 수가 다르면 순서가 밀렸을 수 있어 짝을 짓지 않는다
        same = len(cn) == len(other)
        for i, item in enumerate(cn):
            mate = other[i] if same else None
            # 수가 같아도 중국 서버만 칸 내용을 바꿨을 수 있다 (로딩 팁 '龙门' → '炎国', 그림은 그대로).
            # 긴 글이 하나라도 안 맞으면 제목까지 통째로 짝을 버린다
            if check and isinstance(item, dict) and isinstance(mate, dict) and not _consistent(item, mate):
                mate = None
            yield from walk(item, mate, path + (str(i),), check)


class Collector:
    def __init__(self):
        self.texts: dict[str, collections.Counter] = {}
        self.zh_of: dict[str, str] = {}
        self.src_of: dict[str, str] = {}
        self.templates: dict[str, tuple[str, str, str]] = {}
        self.terms: dict[str, collections.Counter] = {}
        self.term_kind: dict[str, str] = {}
        self.tags: dict[str, set[str]] = {}    # 원문 키 -> 미리 번역 묶음 ('act:act55side', 'char:char_002_amiya')
        self.pairs_seen = 0

    def add(self, zh: str, ko: str | None, src: str, tag: str | None = None) -> None:
        zh = zh.strip()
        if not tn.has_cjk(zh):
            return
        if re.search(r"\{[^{}@]*\}", zh) or re.search(r"\d", zh):
            self._add_template(zh, ko, src)
            if re.search(r"\{[^{}@]*\}", zh):
                return
        k = tn.key(zh)
        if not k:
            return
        bucket = self.texts.setdefault(k, collections.Counter())
        self.zh_of.setdefault(k, zh)
        self.src_of.setdefault(k, src)
        if tag:
            self.tags.setdefault(k, set()).add(tag)
        if ko is not None and usable_ko(zh, ko):
            # 문장 부호를 뺀 키가 같으면 '仓库' 와 대사 '仓库？' 가 한 칸에 모인다.
            # UI·표 문자열이 그 말의 표준 형태이므로 대사보다 훨씬 무겁게 센다
            # 표에 있어도 문장 부호로 끝나면 대사다 (모듈 이야기의 '不……' → '응……')
            spoken = src in DIALOGUE_SOURCES or zh[-1] in SENTENCE_END
            bucket[ko.strip()] += 1 if spoken else 1000
            self.pairs_seen += 1
            # 문단째 들어 있는 글(운영 기록 등)은 화면에 일부만 보이므로 줄 단위로도 넣는다
            zl, kl = _split_lines(zh), _split_lines(ko)
            if len(zl) > 1 and len(zl) == len(kl):
                for a, b in zip(zl, kl):
                    self.add(a, b, src, tag)
        elif src not in ("story", "speaker"):
            # 번역이 없는 글도 줄 단위로 넣어 둔다. 오퍼레이터 파일처럼 화면에는 문단 하나씩 보이므로
            # 통째로만 있으면 OCR 오류를 비슷한 문장 찾기로 바로잡지 못하고, 미리 번역해 둔 것도 못 찾는다
            zl = _split_lines(zh)
            if len(zl) > 1:
                for a in zl:
                    self.add(a, None, src, tag)

    def _add_template(self, zh: str, ko: str | None, src: str) -> None:
        """숫자가 바뀌는 글의 틀. 한국 서버에 없는 틀(ko None)도 넣는다 — 번역 묶음이 번역해 채운다.
        예전에는 버려서 한국 서버에 없는 오퍼레이터의 스킬·재능·모듈 설명은 늘 화면 글자를 기계 번역했고,
        OCR 이 파란 숫자를 틀리게 읽으면('90%' → '0%') 틀린 숫자가 그대로 번역됐다."""
        if ko is not None and not usable_ko(zh, ko):
            ko = None
        k = tn.template_key(zh)
        if tn.SLOT in k and tn.has_cjk(k):
            old = self.templates.get(k)
            if old is None or (old[1] is None and ko is not None):
                self.templates[k] = (zh, ko.strip() if ko is not None else None, src)

    def add_term(self, zh: str, ko: str | None, kind: str) -> None:
        zh = tn.strip_rich(zh).strip()
        if ko is None or not usable_ko(zh, ko):
            return
        ko = tn.strip_rich(ko).strip()
        if not zh or not ko or len(zh) > MAX_TERM_LEN or not tn.has_cjk(zh):
            return
        # 표에 이름으로 적힌 값이 화자 칸보다 믿을 만하다
        # (화자 '整合运动' 은 '리유니온 멤버' 로 옮겨져 있어 단체 이름으로 쓸 수 없다)
        self.terms.setdefault(zh, collections.Counter())[ko] += 1 if kind == "speaker" else 1000
        if kind != "speaker" or zh not in self.term_kind:
            self.term_kind[zh] = kind


def _split_lines(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\\n|\n", text) if p.strip()]


def collect_excel(raw: str, c: Collector, owners: "Owners") -> None:
    cn_dir = os.path.join(raw, "cn", "gamedata", "excel")
    kr_dir = os.path.join(raw, "kr", "gamedata", "excel")
    for name in sorted(os.listdir(cn_dir)):
        table, ext = os.path.splitext(name)
        if ext != ".json" or table in SKIP_TABLES:
            continue
        cn = read_json(os.path.join(cn_dir, name))
        kr_path = os.path.join(kr_dir, name)
        kr = read_json(kr_path) if os.path.exists(kr_path) else {}
        before = c.pairs_seen
        for path, zh, ko in walk(cn, kr):
            c.add(zh, ko, f"excel:{table}", owners.tag(table, path))
            if path and NAME_FIELD.search(path[-1]) and ko is not None:
                c.add_term(zh, ko, table)
        print(f"  {table}: 짝 {c.pairs_seen - before:,}")


def collect_ui(i18n: str, c: Collector) -> None:
    def load(path: str) -> dict[str, str]:
        out = {}
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                m = re.match(r"^\[([^\]]+)\](.*)$", line.rstrip("\r\n"))
                if m:
                    out[m.group(1)] = m.group(2)
        return out

    cn = load(os.path.join(i18n, "string_map_cn.txt"))
    kr = load(os.path.join(i18n, "string_map_kr.txt"))
    before = c.pairs_seen
    for name, zh in cn.items():
        c.add(zh, kr.get(name), "ui")
    print(f"  UI 문구: 짝 {c.pairs_seen - before:,}")


def align(cn: list, kr: list) -> list:
    """중국어 줄마다 대응하는 한국어 줄(없으면 None)을 돌려준다."""
    if not kr:
        return [None] * len(cn)
    sig_a = [(x.kind, bool(x.speaker)) for x in cn]
    sig_b = [(x.kind, bool(x.speaker)) for x in kr]
    if sig_a == sig_b:
        return list(kr)
    out = [None] * len(cn)
    matcher = difflib.SequenceMatcher(None, sig_a, sig_b, autojunk=False)
    for block in matcher.get_matching_blocks():
        for i in range(block.size):
            out[block.a + i] = kr[block.b + i]
    return out


def read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def collect_story(raw: str, c: Collector):
    cn_root = os.path.join(raw, "cn", "gamedata", "story")
    kr_root = os.path.join(raw, "kr", "gamedata", "story")
    files: list[str] = []
    rows: list[tuple] = []
    exact = partial = cn_only = 0
    for folder, _dirs, names in os.walk(cn_root):
        for name in sorted(names):
            if not name.endswith(".txt"):
                continue
            full = os.path.join(folder, name)
            rel = os.path.relpath(full, cn_root).replace("\\", "/")
            cn = storyparse.parse(read_text(full))
            if not cn:
                continue
            kr_path = os.path.join(kr_root, rel)
            kr = storyparse.parse(read_text(kr_path)) if os.path.exists(kr_path) else []
            if not kr:
                cn_only += 1
            elif len(kr) == len(cn):
                exact += 1
            else:
                partial += 1
            pairs = align(cn, kr)
            file_id = len(files)
            files.append(rel)
            for idx, (a, b) in enumerate(zip(cn, pairs)):
                ko = b.text if b is not None and usable_ko(a.text, b.text) else None
                sp_ko = b.speaker if b is not None and b.speaker else None
                c.add(a.text, ko, "story")
                if a.speaker:
                    if sp_ko and usable_ko(a.speaker, sp_ko):
                        c.add_term(a.speaker, sp_ko, "speaker")
                    c.add(a.speaker, sp_ko, "speaker")
                rows.append((file_id, idx, a.kind, a.speaker, sp_ko, a.text, ko, tn.key(a.text)))
    print(f"  스토리: 줄 수 일치 {exact:,} / 일부 정렬 {partial:,} / 중국 서버 전용 {cn_only:,} 파일, 총 {len(rows):,}줄")
    return files, rows


CHAR_ID = re.compile(r"char_\d+_[a-z0-9]+")
ROGUE_TABLES = {"roguelike_topic_table", "roguelike_table"}
SANDBOX_TABLES = {"sandbox_table", "sandbox_perm_table"}
OPERATOR_TABLES = {"handbook_info_table", "charword_table", "uniequip_table"}
# 경로에 이벤트 ID 가 들어 있는 표 ('act55side_01' 작전, 이벤트 화면, 이벤트 아이템, 구역)
EVENT_TABLES = {"activity_table", "stage_table", "item_table", "zone_table"}


class Owners:
    """글이 어느 미리 번역 묶음에 드는지 정한다.

      act:<이벤트 ID>      이벤트 스토리 · 작전 설명 · 이벤트 화면 글
      main:<장 ID>         메인 스토리 한 장
      char:<오퍼레이터 ID>  프로필·파일 자료 · 음성 · 모듈 · 오퍼레이터 기록
      rogue / sandbox      통합전략 / 생명연산
      enemy                적 도감

    표 글은 경로에 든 이벤트 ID('act55side_01')·오퍼레이터 ID('char_102_texas_CN_001')로, 스토리 파일은
    story_review_table(이벤트·메인 스토리)과 handbook_info_table(오퍼레이터 기록)로 가른다.
    표가 없거나 모양이 바뀌었으면 묶음 없이 만든다 (프로그램이 경로·표 이름으로 대강 나눈다).
    """

    def __init__(self, raw: str):
        def table(lang: str, name: str) -> dict:
            try:
                data = read_json(os.path.join(raw, lang, "gamedata", "excel", f"{name}.json"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

        activity = table("cn", "activity_table")
        acts_cn = activity.get("basicInfo") or {}
        acts_kr = table("kr", "activity_table").get("basicInfo") or {}
        review_cn, review_kr = table("cn", "story_review_table"), table("kr", "story_review_table")
        chars_cn, chars_kr = table("cn", "character_table"), table("kr", "character_table")
        self.acts = {a for a, v in acts_cn.items() if isinstance(v, dict)}
        # 이벤트 임무 ID('3D5Activity_37')에는 이벤트 ID 가 없어 missionGroup 으로 찾는다
        self.mission_act = {
            m.get("id"): m["missionGroup"] for m in activity.get("missionData") or []
            if isinstance(m, dict) and m.get("missionGroup") in self.acts
        }
        self.chars = {c for c, v in chars_cn.items() if c.startswith("char_") and isinstance(v, dict)}
        self.equip_char = {
            eid: e["charId"] for eid, e in (table("cn", "uniequip_table").get("equipDict") or {}).items()
            if isinstance(e, dict) and e.get("charId") in self.chars
        }
        self.story: dict[str, str] = {}     # 스토리 경로(소문자, .txt 뺌) -> 묶음
        self.names: dict[str, tuple] = {}   # 묶음 -> (중국어 이름, 한국어 이름, 시작 시각, 신규)

        # 오퍼레이터 기록 ('story_noirc_set_1') 이 누구의 것인지
        record_char = {}
        for cid, entry in (table("cn", "handbook_info_table").get("handbookDict") or {}).items():
            sets = entry.get("handbookAvgList") if isinstance(entry, dict) else None
            for story_set in sets or []:
                if isinstance(story_set, dict) and cid in self.chars:
                    record_char[story_set.get("storySetId")] = cid
        for gid, group in review_cn.items():
            if not isinstance(group, dict):
                continue
            if gid in record_char:
                tag = "char:" + record_char[gid]
            elif group.get("entryType") == "MAINLINE":
                tag = "main:" + gid
            elif group.get("entryType") in ("ACTIVITY", "MINI_ACTIVITY") or gid in self.acts:
                tag = "act:" + gid
            else:
                continue
            if not tag.startswith("char:"):
                other = review_kr.get(gid) if isinstance(review_kr.get(gid), dict) else {}
                self._name(tag, group.get("name"), other.get("name"), group.get("startTime"), not other)
            for info in group.get("infoUnlockDatas") or []:
                txt = info.get("storyTxt") if isinstance(info, dict) else None
                if isinstance(txt, str):
                    self.story.setdefault(txt.lower(), tag)
        for act, info in acts_cn.items():
            if isinstance(info, dict):
                other = acts_kr.get(act) if isinstance(acts_kr.get(act), dict) else {}
                self._name("act:" + act, info.get("name"), other.get("name"), info.get("startTime"), not other)
        self.char_names: list[tuple] = []   # (ID, 중국어 이름, 한국 서버 이름 또는 '') — 이름 바로잡기 사전용
        for cid in self.chars:
            other = chars_kr.get(cid) if isinstance(chars_kr.get(cid), dict) else {}
            self._name("char:" + cid, chars_cn[cid].get("name"), other.get("name"), 0, not other)
            zh, ko, _start, _fresh = self.names["char:" + cid]
            if zh:
                self.char_names.append((cid, zh, ko))

    def _name(self, tag: str, zh, ko, start, fresh: bool) -> None:
        zh = tn.strip_rich(zh).strip() if isinstance(zh, str) else ""
        ko = tn.strip_rich(ko).strip() if isinstance(ko, str) and tn.has_hangul(ko) else ""
        start = int(start) if isinstance(start, (int, float)) else 0
        self.names.setdefault(tag, (zh, ko, start, int(fresh)))

    def tag(self, table: str, path: tuple) -> str | None:
        if table in ROGUE_TABLES:
            return "rogue"
        if table in SANDBOX_TABLES:
            return "sandbox"
        if table == "enemy_handbook_table":
            return "enemy"
        if table in OPERATOR_TABLES:
            for seg in path:
                m = CHAR_ID.match(seg)
                if m and m.group() in self.chars:
                    return "char:" + m.group()
                # 모듈은 'uniequip_002_otter', 모듈 임무는 그 뒤에 번호가 붙는다
                cid = self.equip_char.get(seg) or self.equip_char.get(seg.rsplit("_", 1)[0])
                if cid:
                    return "char:" + cid
            return None
        if table in EVENT_TABLES:
            for seg in path:
                act = self.mission_act.get(seg) or (seg if seg in self.acts else seg.split("_")[0])
                if act in self.acts:
                    return "act:" + act
        return None

    def story_tag(self, rel: str) -> str | None:
        """스토리 파일의 묶음. '[uc]info/' 는 스테이지 고르는 화면의 줄거리 소개라 본편과 같은 묶음이다."""
        base = rel.lower().removesuffix(".txt").removeprefix("[uc]info/")
        tag = self.story.get(base)
        if tag:
            return tag
        parts = base.split("/")
        if parts[0] == "obt" and len(parts) > 2:
            if parts[1].startswith("rogue"):
                return "rogue"
            if parts[1].startswith("sandbox"):
                return "sandbox"
            if parts[1] == "record" and "main:" + parts[2] in self.names:
                return "main:" + parts[2]
        if parts[0] == "activities" and len(parts) > 2 and parts[1] in self.acts:
            return "act:" + parts[1]
        return None

    def group_rows(self, c: "Collector", used: set[str]) -> list[tuple]:
        """(묶음, 보일 이름, 시작 시각, 신규). 한국 서버에 없으면 용어 표의 한국어 이름, 그것도 없으면 중국어 이름."""
        rows = []
        for tag in used:
            zh, ko, start, fresh = self.names.get(tag, ("", "", 0, 0))
            if not ko and zh in c.terms:
                term = c.terms[zh].most_common(1)[0][0]
                ko = term if tn.has_hangul(term) else ""
            rows.append((tag, ko or zh, start, fresh))
        return rows


SCHEMA = """
CREATE TABLE meta(name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE texts(id INTEGER PRIMARY KEY, k TEXT UNIQUE NOT NULL, zh TEXT NOT NULL, ko TEXT, src TEXT);
CREATE TABLE templates(k TEXT PRIMARY KEY, zh TEXT NOT NULL, ko TEXT, src TEXT);
CREATE TABLE terms(zh TEXT PRIMARY KEY, ko TEXT NOT NULL, kind TEXT, n INTEGER);
CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT);
CREATE TABLE story(file_id INTEGER, idx INTEGER, kind TEXT, speaker_zh TEXT, speaker_ko TEXT,
                   zh TEXT, ko TEXT, k TEXT, PRIMARY KEY(file_id, idx)) WITHOUT ROWID;
CREATE INDEX story_k ON story(k);
CREATE TABLE story_tags(file_id INTEGER PRIMARY KEY, tag TEXT);
CREATE TABLE tags(k TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(k, tag)) WITHOUT ROWID;
CREATE TABLE groups(tag TEXT PRIMARY KEY, name TEXT, sort INTEGER, fresh INTEGER);
CREATE TABLE chars(id TEXT PRIMARY KEY, zh TEXT, ko TEXT);
CREATE TABLE recruit_ops(name TEXT PRIMARY KEY, rarity INTEGER, tags TEXT);
CREATE TABLE recruit_tags(name TEXT PRIMARY KEY, id INTEGER);
CREATE VIRTUAL TABLE fts USING fts5(k, tokenize='trigram', content='', detail='none');
"""


def _ambiguous(k: str, bucket: collections.Counter) -> bool:
    """'不。' 는 대사마다 다르게 옮겨져서 맥락 없이 하나를 고르면 틀린다 ('응……').

    이런 짧은 대사는 일반 사전에서 빼고, 스토리를 따라가는 중일 때 그 줄의 번역만 쓴다.
    UI·표 문자열(가중치 1000)은 정해진 번역이므로 해당하지 않는다.
    """
    top = bucket.most_common(1)[0][1]
    return len(k) <= 4 and top < 1000 and top / sum(bucket.values()) < 0.6


def write_db(out: str, c: Collector, files: list[str], rows: list[tuple], story_tags: list[tuple],
             groups: list[tuple], chars: list[tuple], meta: dict, recruit_rows: tuple = ([], [])) -> None:
    tmp = out + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    db.executescript(SCHEMA)
    with db:
        texts = []
        for i, (k, bucket) in enumerate(c.texts.items(), 1):
            ko = bucket.most_common(1)[0][0] if bucket else None
            if bucket and _ambiguous(k, bucket):
                ko = None
            texts.append((i, k, c.zh_of[k], ko, c.src_of[k]))
        db.executemany("INSERT INTO texts VALUES(?,?,?,?,?)", texts)
        # 미리 번역 묶음은 번역이 없는 글에만 필요하다
        untranslated = {t[1] for t in texts if t[3] is None}
        db.executemany("INSERT INTO tags VALUES(?,?)",
                       ((k, tag) for k, tags in c.tags.items() if k in untranslated for tag in tags))
        db.executemany(
            "INSERT INTO fts(rowid, k) VALUES(?,?)",
            ((t[0], t[1]) for t in texts if len(t[1]) >= FTS_MIN),
        )
        db.executemany(
            "INSERT INTO templates VALUES(?,?,?,?)",
            ((k, *v) for k, v in c.templates.items()),
        )
        db.executemany(
            "INSERT INTO terms VALUES(?,?,?,?)",
            (
                (zh, bucket.most_common(1)[0][0], c.term_kind[zh], bucket.most_common(1)[0][1])
                for zh, bucket in c.terms.items()
            ),
        )
        db.executemany("INSERT INTO files VALUES(?,?)", enumerate(files))
        db.executemany("INSERT INTO story VALUES(?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO story_tags VALUES(?,?)", story_tags)
        db.executemany("INSERT INTO groups VALUES(?,?,?,?)", groups)
        db.executemany("INSERT INTO chars VALUES(?,?,?)", chars)
        db.executemany("INSERT OR REPLACE INTO recruit_ops VALUES(?,?,?)", recruit_rows[0])
        db.executemany("INSERT OR REPLACE INTO recruit_tags VALUES(?,?)", recruit_rows[1])
        db.executemany("INSERT INTO meta VALUES(?,?)", ((k, str(v)) for k, v in meta.items()))
    db.execute("INSERT INTO fts(fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, out)


RARE_TEXTS = 5   # 이만큼 이하의 글에만 나오는 한자는 OCR 이 아이콘을 잘못 읽은 것일 가능성이 크다 ('囝' 은 52만 글 중 1)


def rare_chars(keys) -> str:
    """게임 글 전체에서 거의 안 쓰이는 한자. 글자 앞에 붙은 이런 글자는 아이콘을 읽은 것으로 보고 떼어 본다."""
    counts = collections.Counter()
    for k in keys:
        counts.update(set(k))
    return "".join(sorted(ch for ch, n in counts.items() if "㐀" <= ch <= "鿿" and n <= RARE_TEXTS))


def build(raw: str, i18n: str, out: str, revision: str = "") -> dict:
    started = time.time()
    c = Collector()
    owners = Owners(raw)
    print("엑셀 표 읽는 중")
    collect_excel(raw, c, owners)
    print("UI 문구 읽는 중")
    collect_ui(i18n, c)
    print("스토리 읽는 중")
    files, rows = collect_story(raw, c)
    story_tags = [(i, tag) for i, rel in enumerate(files) if (tag := owners.story_tag(rel))]
    used = {tag for _i, tag in story_tags} | {tag for tags in c.tags.values() for tag in tags}
    groups = owners.group_rows(c, used)
    print(f"  미리 번역 묶음 {len(groups):,}개, 묶음이 정해진 스토리 파일 {len(story_tags):,}개")

    with_ko = sum(1 for b in c.texts.values() if b)
    charset = {ch for k in c.texts for ch in k if "㐀" <= ch <= "鿿"}
    meta = {
        "revision": revision,
        "charset": "".join(sorted(charset)),
        "rare": rare_chars(c.texts),
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "texts": len(c.texts),
        "texts_with_ko": with_ko,
        "templates": len(c.templates),
        "templates_cn": sum(1 for _zh, ko, _src in c.templates.values() if ko is None),
        "terms": len(c.terms),
        "story_lines": len(rows),
        "story_files": len(files),
    }
    # 공개모집 계산용 표 (중국 서버 모집 목록). 성별은 오퍼레이터 프로필 글의 【性别】 에서 찾는다
    excel = os.path.join(raw, "cn", "gamedata", "excel")
    ops, tag_rows, rule = recruit.build_rows(read_json(os.path.join(excel, "gacha_table.json")),
                                             read_json(os.path.join(excel, "character_table.json")),
                                             c.zh_of.values())
    meta["recruit_rule"] = json.dumps(rule, ensure_ascii=False)
    print(f"  공개모집 오퍼레이터 {len(ops)}명, 태그 {len(tag_rows)}개")
    write_db(out, c, files, rows, story_tags, groups, owners.char_names, meta, (ops, tag_rows))
    shown = {k: v for k, v in meta.items() if k not in ("charset", "rare")}
    print(f"완료 {time.time() - started:.0f}초, {os.path.getsize(out) / 1e6:.0f}MB: {shown}")
    return meta


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--i18n", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--revision", default="")
    args = ap.parse_args()
    build(args.raw, args.i18n, args.out, args.revision)


if __name__ == "__main__":
    main()
