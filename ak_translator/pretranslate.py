"""분류별 미리 번역. 공식 번역이 없는 글을 게임하기 전에 번역해 번역캐시에 넣어 둔다.

  이벤트        이벤트 하나의 스토리·작전 설명·이벤트 화면 글 (메인 스토리 장, 적 도감도 여기서 고른다)
  통합전략      통합전략 표(유물·선택지·조우 설명)와 통합전략 스토리(엔딩·월간 기록)
  오퍼레이터    오퍼레이터 하나의 파일(신뢰도로 여는 프로필·파일 자료)·음성·모듈·오퍼레이터 기록.
                한국 서버에 아직 없는 오퍼레이터를 한꺼번에 고를 수도 있다
  생명연산      생명연산 표

어느 글이 어느 묶음인지는 DB 를 만들 때 정한다 (build_db.Owners). 그 표가 없는 예전 DB 면 스토리는 경로로,
표 글은 표 이름으로 대강 나눈다. 나머지 글(메달·상점 등)은 평소처럼 화면에 나올 때 번역한다.
번역문은 화면에서 번역한 것과 똑같이 번역캐시.md 에 쌓이므로 고치는 방법도 같다.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from . import mt as mt_mod
from . import textnorm as tn
from .resolver import _clean

BATCH_CHARS = 5000       # 한 요청에 보낼 글자 수 상한 (DeepL 요청 크기 제한 128KB 보다 한참 작게)
PAUSE_BATCH = 0.5        # 여러 줄씩 보내는 번역기(DeepL)의 요청 간격
PAUSE_SINGLE = 1.0       # 한 줄씩 보내는 번역기(구글 무료)는 막히지 않게 더 쉬어 간다
RETRY_WAIT = 15.0
MAX_FAILS = 3
SMALL_GROUP = 30         # 이보다 줄이 적은 이벤트 묶음은 '그 밖의 스토리' 로 모은다
FRESH = "fresh"          # 한국 서버에 아직 없는 오퍼레이터 전부


@dataclass
class Group:
    key: str
    label: str
    items: list = field(default_factory=list)   # (원문, 스토리 파일 번호 또는 -1)
    order: tuple = ()
    fresh: bool = False                         # 오퍼레이터: 한국 서버에 아직 없다

    def left(self, cache) -> list:
        """아직 번역캐시에 없는 것."""
        return [it for it in self.items if not cache.get(tn.key(it[0]))]


@dataclass
class Catalog:
    events: list         # 이벤트·메인 스토리 장·적 도감·그 밖의 스토리
    operators: list      # 맨 앞은 '신규 오퍼레이터 전부' (있으면)
    rogue: Group
    sandbox: Group
    named: bool          # 묶음 표가 있는 DB 다 (이벤트·오퍼레이터 이름이 나온다)
    file_group: dict     # {스토리 파일 번호: 이벤트 묶음 key} — 지금 읽는 스토리의 이벤트를 고르려고


def load(db) -> Catalog:
    """DB 에서 묶음별로 번역할 글을 모은다. 2초쯤 걸리므로 뒤에서 부른다."""
    info = db.group_info()
    file_tags = db.story_tags()
    groups: dict[str, Group] = {}
    seen: set[tuple[str, str]] = set()     # (묶음, 원문 키). 같은 줄이 여러 묶음에 있으면 각각 넣는다

    def group(tag: str) -> Group:
        g = groups.get(tag)
        if g is None:
            label, order, fresh = _describe(tag, info.get(tag))
            g = groups[tag] = Group(tag, label, order=order, fresh=fresh)
        return g

    file_group: dict[int, str] = {}
    for file_id, path, zh in db.untranslated_story():
        tag = file_tags.get(file_id) or _path_tag(path)
        _add(group(tag), zh, file_id, seen)
        file_group[file_id] = tag
    for zh, tag in db.untranslated_tagged():
        _add(group(tag), zh, -1, seen)

    events = [g for g in groups.values() if _is_event(g.key)]
    # 옛 이벤트에 한두 줄씩 남은 것은 목록만 길게 하므로 하나로 모은다
    small = [g for g in events if len(g.items) < SMALL_GROUP and g.key != "enemy"]
    if len(small) > 1:
        rest = groups.get("etc") or group("etc")
        for g in small:
            if g is rest:
                continue
            have = {tn.key(zh) for zh, _ in rest.items}
            rest.items += [it for it in g.items if tn.key(it[0]) not in have]
            del groups[g.key]
        file_group = {f: (t if t in groups else "etc") for f, t in file_group.items()}
        events = [g for g in groups.values() if _is_event(g.key)]

    operators = sorted((g for g in groups.values() if g.key.startswith("char:")), key=lambda g: g.order)
    # 한국 서버에 있는 오퍼레이터 대부분은 짧은 음성 몇 줄만 남아 있다. 그런 것은 한 줄로 모으고,
    # 새 모듈·오퍼레이터 기록처럼 많이 남은 오퍼레이터만 따로 보인다
    small = [g for g in operators if not g.fresh and len(g.items) < SMALL_GROUP]
    if len(small) > 1:
        rest = Group("char:etc", f"그 밖의 오퍼레이터 ({len(small)}명, 몇 줄씩)", order=(9,))
        for g in small:
            rest.items += g.items
        merged = {g.key for g in small}
        operators = [g for g in operators if g.key not in merged] + [rest]
    fresh = [g for g in operators if g.fresh]
    if fresh:
        bundle = Group(FRESH, f"신규 오퍼레이터 전부 ({len(fresh)}명)", order=(-1,))
        for g in fresh:
            bundle.items += g.items
        operators.insert(0, bundle)
    return Catalog(sorted(events, key=lambda g: g.order), operators,
                   groups.get("rogue") or Group("rogue", "통합전략"),
                   groups.get("sandbox") or Group("sandbox", "생명연산"),
                   bool(info), file_group)


def _is_event(tag: str) -> bool:
    return tag.startswith(("act:", "main:")) or tag in ("etc", "enemy")


def _add(group: Group, zh: str, file_id: int, seen: set) -> None:
    # 화면에서 찾을 때와 같은 모양으로 다듬어야 번역캐시에서 찾힌다 (resolver 가 _clean 한 원문으로 찾는다)
    text = _clean(zh)
    mark = (group.key, tn.key(text))
    if tn.has_cjk(text) and mark not in seen:
        seen.add(mark)
        group.items.append((text, file_id))


def _path_tag(path: str) -> str:
    """묶음 표에 없는 스토리 파일을 경로로 나눈다 ('[uc]info/' 는 스테이지 고르는 화면의 줄거리 소개다)."""
    parts = path.removeprefix("[uc]info/").split("/")
    if parts[0] == "obt" and len(parts) > 1:
        if parts[1].startswith("rogue"):
            return "rogue"
        if parts[1] == "sandbox":
            return "sandbox"
        if parts[1] == "memory":
            return "char:"
        if parts[1] == "main":
            return "main:"
    if parts[0] == "activities" and len(parts) > 2:
        return "act:" + parts[1]
    return "etc"


def _describe(tag: str, named) -> tuple[str, tuple, bool]:
    """(보일 이름, 정렬 순서, 신규 오퍼레이터인가). 최근 이벤트, 신규 오퍼레이터가 위로 온다."""
    name, sort, fresh = named if named else ("", 0, 0)
    kind, _, ident = tag.partition(":")
    if kind == "act":
        m = re.search(r"\d+", ident)
        # 시작 시각이 없으면 이벤트 번호로 (act55side 가 act54side 보다 최근)
        return name or f"이벤트 {ident}", (0, -(sort or (int(m.group()) if m else 0))), False
    if kind == "main":
        m = re.search(r"\d+", ident)
        return name or "메인 스토리", (1, -(int(m.group()) if m else 0)), False
    if kind == "char":
        if not ident:
            return "오퍼레이터 전부", (0,), False
        return name or ident, (0 if fresh else 1, name or ident), bool(fresh)
    fixed = {"enemy": ("적 도감 (한국 서버에 없는 적)", (2,)), "etc": ("그 밖의 스토리", (9,)),
             "rogue": ("통합전략", (0,)), "sandbox": ("생명연산", (0,))}
    label, order = fixed.get(tag, (tag, (9,)))
    return label, order, False


class Runner(threading.Thread):
    """한 묶음을 번역한다. 화면 번역(resolver 의 일꾼)과 따로 돌아서 게임 번역을 막지 않는다."""

    def __init__(self, resolver, group: Group):
        super().__init__(name="pretranslate", daemon=True)
        self.r = resolver
        self.group = group
        self.items = group.left(resolver.cache)
        self.total = len(self.items)
        self.done = 0
        self.chars = 0
        self.state = "시작하는 중"
        self.finished = False
        self._halt = threading.Event()
        self._speakers: dict[int, set[str]] = {}

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        fails = 0
        i = 0
        try:
            while i < len(self.items) and not self._halt.is_set():
                batch = self._next_batch(i)
                self.state = "번역 중"
                try:
                    results = self.r.translate_many([zh for zh, _ in batch], self._speakers_of(batch))
                except mt_mod.QuotaExhausted:
                    self.state = "번역기 한도를 다 써서 멈췄습니다"
                    return
                except Exception as exc:
                    fails += 1
                    if fails >= MAX_FAILS:
                        self.state = f"번역기가 계속 실패해 멈췄습니다 ({type(exc).__name__})"
                        return
                    self.state = f"번역기가 응답하지 않아 {RETRY_WAIT:.0f}초 뒤 다시 보냅니다"
                    self._halt.wait(RETRY_WAIT)
                    continue
                fails = 0
                for (zh, _f), (ko, label) in zip(batch, results):
                    self.r.cache.put(zh, ko, label)
                i += len(batch)
                self.done = i
                self.chars += sum(len(zh) for zh, _ in batch)
                self.r.on_ready()   # 이 글이 지금 화면에 번역을 기다리며 떠 있으면 바로 채운다
                self._halt.wait(PAUSE_BATCH if len(batch) > 1 else PAUSE_SINGLE)
            self.state = "멈췄습니다" if self._halt.is_set() else "끝났습니다"
        finally:
            self.finished = True

    def _next_batch(self, start: int) -> list:
        """번역기가 받는 만큼 묶는다. 오퍼레이터 기록은 한두 줄짜리 파일이 많아 파일을 넘나들며 묶는다."""
        size = self.r.engine.batch
        first = self.items[start]
        batch, chars = [first], len(first[0])
        for item in self.items[start + 1:start + size]:
            if chars + len(item[0]) > BATCH_CHARS:
                break
            batch.append(item)
            chars += len(item[0])
        return batch

    def _speakers_of(self, batch: list) -> set[str]:
        """묶음에 든 스토리 파일들의 화자. 두 글자 이름은 그 스토리의 화자일 때만 용어로 지킨다."""
        out: set[str] = set()
        for file_id in {f for _zh, f in batch if f >= 0}:
            if file_id not in self._speakers:
                self._speakers[file_id] = self.r.db.file_speakers(file_id)
            out |= self._speakers[file_id]
        return out
