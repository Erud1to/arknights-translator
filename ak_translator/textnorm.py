"""원문 문자열을 찾아보기용 키로 정규화한다.

OCR 은 문장 부호(， vs ,)와 줄바꿈을 자주 다르게 읽는다. 그래서 문장 부호·공백을 전부
빼고 글자와 숫자만 남긴 키로 비교한다. 게임 데이터 쪽과 OCR 쪽이 같은 함수를 지나야
서로 맞아떨어진다.
"""
from __future__ import annotations

import re
import unicodedata

# <@ba.kw>...</>, <color=#fff>, <$ba.liftoff>...</> ($ 꼴은 용어 설명이 붙는 글자다. 빠뜨리면 키에 'baliftoff' 가 남아
# OCR 글자와 못 맞추고, 번역문에도 태그가 그대로 보인다)
_RICH = re.compile(r"</?[@$]?[A-Za-z][^<>]*>|</>")
_STORY_VAR = re.compile(r"\{@[A-Za-z_]+\}")            # {@nickname}
_TEMPLATE = re.compile(r"\{[^{}]*\}")                  # {atk:0%}, {0}
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
# 화면 숫자 (앞의 부호까지). '15-20' 의 '-' 는 범위 표시이므로 숫자 바로 뒤의 '-' 는 부호로 보지 않는다
_SIGNED_NUMBER = re.compile(r"(?:(?<!\d)-)?\d+(?:\.\d+)?%?")
_SIGN_BEFORE = "-"      # 틀에서 자리표시자 바로 앞에 글자로 적힌 빼기 부호 (+ 는 값이 음수일 수 있어 해당 없음)
_CJK_RE = re.compile(r"[㐀-鿿]")
_HANGUL_RE = re.compile(r"[가-힣]")

NICKNAME = "{@nickname}"
SLOT = "#"


def strip_rich(text: str) -> str:
    """리치 텍스트 태그를 뗀다. 글자는 남긴다."""
    return _RICH.sub("", text or "")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def has_hangul(text: str) -> bool:
    return bool(_HANGUL_RE.search(text or ""))


def cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def key(text: str) -> str:
    """정확 일치용 키: 글자·숫자만 남긴다."""
    text = strip_rich(text)
    text = _STORY_VAR.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if ch.isalnum()).lower()


_SPLIT_DECIMAL = re.compile(r"(?<=\d)\.\s+(?=\d)")     # OCR 이 '1.0' 을 '1. 0' 으로 읽는다 (2026-11-10 위수협의 캡처)


def template_key(text: str) -> str:
    """숫자가 바뀌는 문장(스킬 설명 등)을 위한 키.

    '공격력 +{atk:0%}' 와 화면의 '공격력 +60%' 가 같은 키가 되도록, 자리표시자와 숫자를
    모두 # 하나로 바꾼다.
    """
    text = strip_rich(text)
    text = _STORY_VAR.sub("", text)
    text = _SPLIT_DECIMAL.sub(".", text)
    text = _TEMPLATE.sub(SLOT, text)
    text = unicodedata.normalize("NFKC", text)
    text = _NUMBER.sub(SLOT, text)
    return "".join(ch for ch in text if ch.isalnum() or ch == SLOT).lower()


def slots_of(text: str) -> list[str]:
    """문장에 나오는 자리표시자와 숫자를 등장 순서대로 뽑는다. template_key 와 같은 순서."""
    return [token for token, _before in _slot_marks(text)]


def _slot_marks(text: str) -> list[tuple[str, str]]:
    """(자리표시자 또는 숫자, 그 바로 앞 글자). slots_of 와 같은 순서."""
    text = strip_rich(text)
    text = _STORY_VAR.sub("", text)
    return [(m.group(0), text[m.start() - 1] if m.start() else "")
            for m in re.finditer(r"\{[^{}]*\}|\d+(?:\.\d+)?%?", text)]


def numbers_of(text: str) -> list[str]:
    """화면 글자의 숫자들. 음수는 '-' 를 붙여 돌려준다 ('攻击力-70%' → '-70%')."""
    return _SIGNED_NUMBER.findall(_SPLIT_DECIMAL.sub(".", unicodedata.normalize("NFKC", text).replace("−", "-")))


_ZH_CLAUSE = re.compile(r"(?<=[，；。！？])")
_KO_CLAUSE = re.compile(r"(?<=[,;.!?])\s+")


def clause_slice(zh_template: str, ko_template: str, part: str, first: int) -> tuple[str, str, int] | None:
    """줄이 잘려 틀의 일부만 보일 때, 보이는 절(쉼표 단위)만 중국어·한국어 틀에서 잘라 낸다. (중국어 조각, 한국어 조각,
    조각 안에서 보이는 첫 숫자 자리 번호). 두 틀의 절 수가 다르면 None (어느 절이 어느 절인지 모른다).
    part 는 보이는 글의 틀 키, first 는 틀 전체에서 보이는 첫 자리 번호."""
    zt, kt = strip_rich(zh_template), strip_rich(ko_template)
    zc = [c for c in _ZH_CLAUSE.split(zt) if c]
    kc = [c for c in (x.strip() for x in _KO_CLAUSE.split(kt)) if c]
    if len(zc) != len(kc) or len(zc) < 2:
        return None
    counts = [len(slots_of(c)) for c in zc]
    before = [sum(counts[:i]) for i in range(len(zc))]
    # 보이는 첫 자리가 든 절. 자리가 하나도 안 보이면 절을 정할 수 없다
    start = next((i for i in range(len(zc)) if before[i] + counts[i] > first), None)
    if start is None:
        return None
    # 앞뒤로 자리 없는 절이 통째로 보이면 함께 (예: '攻击范围扩大，' 가 앞에 보인다)
    while start > 0 and template_key(zc[start - 1]) and template_key(zc[start - 1]) in part:
        start -= 1
    end = start
    while end + 1 < len(zc) and (before[end + 1] < first + part.count(SLOT)
                                 or (template_key(zc[end + 1]) and template_key(zc[end + 1]) in part)):
        end += 1
    return "".join(zc[start:end + 1]), " ".join(kc[start:end + 1]), first - before[start]


def fill_template(zh_template: str, ko_template: str, ocr_text: str, first: int | None = None) -> str | None:
    """한국어 틀의 {자리표시자}를 화면에서 읽은 숫자로 채운다.

    중국어와 한국어는 어순이 달라 위치로 맞출 수 없다. 그래서 자리표시자 이름으로 찾는다.
    화면의 숫자 개수가 틀과 다르면 잘못 채우느니 None 을 돌려준다.
    """
    marks = _slot_marks(zh_template)
    values = numbers_of(strip_rich(ocr_text))
    # first 를 주면 화면 글이 틀의 일부만 담은 것이다 (줄이 잘림). first 번째 자리부터 보이는 숫자만큼만 채우고,
    # 안 보이는 자리는 '…' 로 둔다 (2026-11-10 위수협의 스킬 설명)
    covered = marks if first is None else marks[first:first + len(values)]
    if len(covered) != len(values):
        return None
    table = {}
    fixed = []
    for (slot, before), value in zip(covered, values):
        # 틀에 '-{value}' 처럼 부호가 글자로 적혀 있으면 부호는 틀이 맡는다. 화면의 '-70%' 에서 '-' 를 또 쓰면 '--70%' 가 된다.
        # 틀에 부호가 없는 '{atk:0%}' 는 값 자체가 음수라 화면의 '-' 가 값에 붙어야 한다 ('-70%' 가 '70%' 로 나왔다, 2026-11-10)
        if value.startswith("-") and before and before in _SIGN_BEFORE:
            value = value[1:]
        if slot.startswith("{"):
            table[slot] = value
        elif slot != value:
            fixed.append((slot, value))
    if first is not None:
        for slot, _before in marks:
            if slot.startswith("{"):
                table.setdefault(slot, "…")
    out = _TEMPLATE.sub(lambda m: table.get(m.group(0), "…" if first is not None else m.group(0)), strip_rich(ko_template))
    out = re.sub(r"\+\s*(?=-\d)", "", out)           # 한국어 틀의 '+{atk}' 에 음수가 들어가면 '+-30%' 가 된다
    # 틀에 고정 숫자로 박힌 값이 화면과 다르면(강화 단계별 설명 등) 그 숫자도 화면 값으로 바꾼다
    for old, new in fixed:
        pattern = r"(?<![\d.])" + re.escape(old) + r"(?![\d.])"
        out, count = re.subn(pattern, new, out, count=1)
        if not count:
            return None
    return out


def display_ko(text: str, nickname: str = "박사") -> str:
    """공식 한국어 문장을 화면에 보일 형태로 다듬는다."""
    text = strip_rich(text).replace("\\n", "\n")
    return text.replace(NICKNAME, nickname)
