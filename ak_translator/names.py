"""OCR 이 한두 글자 잘못 읽은 오퍼레이터 이름을 이름 사전에서 찾는다.

오퍼레이터 목록의 이름은 문장이 아니라서 문장 사전(퍼지 검색)으로는 못 찾고, 번역기에 보내면
'棘刺' 를 '束束刺' 로 읽은 것이 '가시 뭉치' 가 된다. 이름은 수가 정해져 있으니 다음 세 경우를 본다.

  - 반쪽으로 갈라 읽은 글자 '?' 가 있다     '霜卩十' → '霜?' → 霜叶 / 霜华 (모양으로 고른다)
  - 한 글자를 다른 글자로 읽었다 (4자 이상)  '祜天寺若麦' → '祐天寺若麦'
  - 앞뒤 글자를 놓쳤다 (3자 이상)           '莱门莎' → '克莱门莎', '明霄陈' → '赤刃明霄陈'
  - 가운데 한 글자를 놓쳤다 (3자 이상)       '罗比塔' → '罗比菈塔'

후보가 하나일 때만(또는 글자 모양으로 하나를 고를 수 있을 때만) 쓴다.
"""
from __future__ import annotations

import re
from collections import defaultdict

MAX_LEN = 8
MIN_LEN = 3     # 앞뒤 글자를 놓친 이름은 이만큼은 읽혀야 찾는다
SWAP_LEN = 4    # 한 글자 오독은 이만큼 길어야 본다. 세 글자는 한 자만 달라도 다른 말이다 ('执行者' / '见行者')
_SENTENCE = re.compile(r"[.!?'\"“”‘’…。！？]")


def _ideographs(text: str) -> str:
    return "".join(ch for ch in text if "㐀" <= ch <= "鿿" or ch == "?")


def _fits(name: str, pattern: str) -> bool:
    return all(p == "?" or p == ch for p, ch in zip(pattern, name))


def load_names(db, pack=None) -> list[tuple[str, str]]:
    """오퍼레이터 이름 (중국어, 한국어).

    게임 데이터의 캐릭터 표(DB 의 chars)가 있으면 그것을 쓴다. 한국 서버에 아직 없는 오퍼레이터는 번역 묶음에
    든 이름을 쓰고, 그것도 없으면 이름 사전에서 뺀다. 캐릭터 표가 없는 예전 DB 면 비워 둔다 (공식 데이터를 갱신하면 생긴다).
    """
    rows = db.char_names()
    if rows is None:
        return []
    from . import textnorm as tn

    out = []
    for zh, ko in rows:
        if not ko and pack is not None:
            # 배포자가 정한 이름(names.tsv)이 먼저. 없으면 같은 글자의 묶음 줄 (대사 줄이면 아래에서 빠진다)
            named = pack.name(tn.key(zh)) if hasattr(pack, "name") else None
            found = pack.get(tn.key(zh))
            ko = named or (found[0] if found else "")
        if ko and not _SENTENCE.search(ko):
            out.append((zh, ko))
    return out


class NameIndex:
    def __init__(self, pairs):
        self.entries: dict[str, tuple[str, str]] = {}   # 한자만 남긴 이름 -> (원래 이름, 한국어)
        self.single: dict[str, str] = {}                 # 한 글자 이름 -> 한국어 ('宴' → 우타게)
        for zh, ko in pairs:
            key = _ideographs(zh or "").replace("?", "")
            if ko and 2 <= len(key) <= MAX_LEN:
                self.entries.setdefault(key, (zh, ko))
            elif ko and len(key) == 1:
                self.single.setdefault(key, ko)
        self._by_len: dict[int, list[str]] = defaultdict(list)
        for key in self.entries:
            self._by_len[len(key)].append(key)

    def candidates(self, text: str, masked: str = "") -> tuple[list[str], list[int]]:
        """(맞을 수 있는 이름들, '?' 자리 번호). 없으면 빈 목록."""
        pattern = _ideographs(masked)
        if "?" in pattern:
            found = [k for k in self._by_len.get(len(pattern), ()) if _fits(k, pattern)]
            if found:
                return found, [i for i, ch in enumerate(pattern) if ch == "?"]
        key = _ideographs(text).replace("?", "")
        n = len(key)
        if n < MIN_LEN or n > MAX_LEN or key in self.entries:
            return [], []
        if n >= SWAP_LEN:
            found = [k for k in self._by_len.get(n, ()) if sum(a != b for a, b in zip(k, key)) == 1]
            if found:
                return found, []
        found = [k for m in (n + 1, n + 2) for k in self._by_len.get(m, ()) if key in k]
        if found:
            return found, []
        # 가운데 한 글자를 놓쳤다 ('罗比菈塔' 의 드문 글자 '菈' 를 못 읽어 '罗比塔')
        return [k for k in self._by_len.get(n + 1, ()) if any(k[:i] + k[i + 1:] == key for i in range(1, n))], []

    def missed(self, text: str, key: str) -> tuple[int, int]:
        """이름 key 가운데 화면 글자에서 빠진 앞뒤 글자 수."""
        seen = _ideographs(text).replace("?", "")
        at = key.find(seen) if len(key) > len(seen) else -1
        return (at, len(key) - at - len(seen)) if at >= 0 else (0, 0)

    def is_name(self, text: str) -> bool:
        key = _ideographs(text).replace("?", "")
        return key in self.entries or key in self.single

    def ko(self, key: str) -> str:
        return self.entries[key][1]

    def zh(self, key: str) -> str:
        return self.entries[key][0]
