"""기계 번역에서 공식 용어를 지킨다.

그냥 번역기에 넣으면 '阿米娅' 가 'Amiya', '罗德岛' 가 '로드 아일랜드' 가 된다. 용어를 번역기가
건드리지 못하는 토큰으로 바꿔 보낸 뒤, 돌아온 문장에서 공식 이름으로 되돌린다.

실측 (구글 번역, 네 문장):
  한글을 문장에 직접 삽입  '로도스 주식', '은애쉬', '차트레이터' 로 망가진다
  [1] 같은 번호            이름은 살지만 문장 구조가 깨진다 ('[1], [3] 중 [2]를')
  XZAQ 같은 라틴 토큰       전부 온전함. 이것을 쓴다

토큰 뒤에 붙은 조사는 토큰 기준이라 틀리므로('오퍼레이터이', '근위국를') 되돌릴 때 받침을 보고 고친다.
"""
from __future__ import annotations

import re
import string

from . import textnorm as tn
from .seed_terms import SEED_TERMS

MAX_LEN = 16

# 이름이 아닌 값도 섞여 있는 표는 제외하고, 믿을 수 있는 표만 쓴다.
# 표마다 쓸 수 있는 최소 글자 수. 2글자 이름에는 일반 단어가 많다
# ('真理' 는 이스티나이기도 하고 그냥 '진리' 이기도 하다).
KIND_MIN_LEN = {
    "character_table": 3,
    "handbook_team_table": 2,
    "campaign_table": 2,
    "zone_table": 3,
    "enemy_handbook_table": 3,
    "item_table": 3,
    "speaker": 3,
}
_TOKEN = re.compile(r"XZ([A-Z]{1,2})Q", re.I)


def _tokens():
    for ch in string.ascii_uppercase:
        yield f"XZ{ch}Q"
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            yield f"XZ{a}{b}Q"


def _batchim(word: str) -> tuple[bool, bool]:
    """(받침이 있는가, 그 받침이 ㄹ 인가). 글자로 판단할 수 없으면 받침 없음으로 친다."""
    for ch in reversed(word):
        if ch.isalnum():
            if "가" <= ch <= "힣":
                tail = (ord(ch) - 0xAC00) % 28
                return tail != 0, tail == 8
            if ch.lower() in "lmnr" or ch in "0136":
                return True, ch.lower() in "lr" or ch == "1"
            return False, False
    return False, False


_PAIRS = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"),
          "을": ("을", "를"), "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와"),
          "이라고": ("이라고", "라고"), "라고": ("이라고", "라고"),
          "이나": ("이나", "나"), "이랑": ("이랑", "랑"), "랑": ("이랑", "랑")}
_COPULA_NEXT = set("다며고라지야어었에네니면나")


def _fix_particle(word: str, josa: str, following: str) -> str:
    has, rieul = _batchim(word)
    if josa in ("으로", "로"):
        return "으로" if has and not rieul else "로"
    if josa == "이" and following[:1] in _COPULA_NEXT:
        return josa     # '이다' 는 조사 '이' 가 아니다
    pair = _PAIRS.get(josa)
    if pair is None:
        return josa
    return pair[0] if has else pair[1]


_RESTORE = re.compile(r"XZ[A-Z]{1,2}Q(이라고|라고|이나|이랑|으로|은|는|이|가|을|를|과|와|랑|로)?", re.I)


class Glossary:
    def __init__(self, db, user_store):
        self.user = user_store
        self.base: dict[str, str] = {}
        self.short: dict[str, str] = {}     # 지금 스토리의 화자일 때만 쓰는 이름
        for zh, ko, kind, n in db.all_terms():
            need = KIND_MIN_LEN.get(kind)
            if need is None or len(zh) > MAX_LEN:
                continue
            if kind == "speaker" and n < 3:
                continue
            if len(zh) >= need:
                self.base[zh] = ko
            elif len(zh) >= 2 or kind == "character_table":
                self.short[zh] = ko
        self.base.update(SEED_TERMS)

    def add_names(self, pairs) -> None:
        """오퍼레이터 이름 (중국어, 한국어) 을 더한다. 용어 표의 kind 는 그 이름이 처음 나온 표라서
        '阿米娅'(display_meta_table), '凯尔希'(handbook_info_table) 같은 주요 인물이 빠져 있었다.
        character_table 과 같은 기준으로 넣는다: 3자 이상은 늘, 2자는 지금 스토리의 화자일 때만.
        이미 있는 용어는 바꾸지 않는다. 다른 스레드가 읽는 중이므로 새 표를 만들어 바꿔 끼운다."""
        base, short = dict(self.base), dict(self.short)
        for zh, ko in pairs:
            if len(zh) >= KIND_MIN_LEN["character_table"]:
                base.setdefault(zh, ko)
            elif len(zh) == 2:
                short.setdefault(zh, ko)
        self.base, self.short = base, short

    def add_terms(self, pairs) -> None:
        """용어 표 밖의 공식 이름을 더한다 (통합전략 유물·이벤트 이름, OfficialDB.rogue_names).
        이미 있는 용어는 바꾸지 않는다. add_names 처럼 새 표를 만들어 바꿔 끼운다."""
        base = dict(self.base)
        for zh, ko in pairs:
            base.setdefault(zh, ko)
        self.base = base

    def _table(self, speakers: set[str]) -> dict[str, str]:
        table = dict(self.base)
        for zh in speakers:
            if zh in self.short:
                table[zh] = self.short[zh]
        for k, ko in self.user.items():
            table[k] = ko
        return table

    def found(self, text: str, speakers: set[str] | None = None) -> dict[str, str]:
        """text 에 나오는 용어 {중국어: 공식 이름}. Gemini 에 용어집으로 보낸다."""
        table = self._table(speakers or set())
        out: dict[str, str] = {}
        i, n = 0, len(text)
        while i < n:
            for size in range(min(MAX_LEN, n - i), 1, -1):
                ko = table.get(text[i:i + size])
                if ko:
                    out[text[i:i + size]] = ko
                    i += size
                    break
            else:
                i += 1
        return out

    def protect(self, text: str, speakers: set[str] | None = None) -> tuple[str, dict[str, str]]:
        """(토큰으로 바꾼 문장, {토큰: 공식 이름}). 긴 용어를 먼저 맞춘다."""
        table = self._table(speakers or set())
        out = []
        mapping: dict[str, str] = {}
        used: dict[str, str] = {}
        gen = _tokens()
        i = 0
        n = len(text)
        while i < n:
            for size in range(min(MAX_LEN, n - i), 1, -1):
                piece = text[i:i + size]
                ko = table.get(piece)
                if ko:
                    token = used.get(piece)
                    if token is None:
                        token = next(gen)
                        used[piece] = token
                        mapping[token] = ko
                    # 중국어는 띄어쓰기가 없어 토큰이 앞말에 붙으면 한 단어로 오해받는다
                    out.append(f" {token} ")
                    i += size
                    break
            else:
                out.append(text[i])
                i += 1
        return re.sub(r" {2,}", " ", "".join(out)).strip(), mapping

    @staticmethod
    def restore(translated: str, mapping: dict[str, str]) -> str | None:
        """토큰을 공식 이름으로 되돌린다. 토큰이 사라졌으면 None."""
        upper = {t.upper(): ko for t, ko in mapping.items()}
        found = set()

        def repl(m: re.Match) -> str:
            whole = m.group(0)
            josa = m.group(1) or ""
            token = whole[:len(whole) - len(josa)].upper()
            ko = upper.get(token)
            if ko is None:
                return whole
            found.add(token)
            if not josa:
                return ko
            following = m.string[m.end():m.end() + 1]
            return ko + _fix_particle(ko, josa, following)

        out = _RESTORE.sub(repl, translated)
        if found != set(upper) or _TOKEN.search(out):
            return None
        return out
