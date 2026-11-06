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


def template_key(text: str) -> str:
    """숫자가 바뀌는 문장(스킬 설명 등)을 위한 키.

    '공격력 +{atk:0%}' 와 화면의 '공격력 +60%' 가 같은 키가 되도록, 자리표시자와 숫자를
    모두 # 하나로 바꾼다.
    """
    text = strip_rich(text)
    text = _STORY_VAR.sub("", text)
    text = _TEMPLATE.sub(SLOT, text)
    text = unicodedata.normalize("NFKC", text)
    text = _NUMBER.sub(SLOT, text)
    return "".join(ch for ch in text if ch.isalnum() or ch == SLOT).lower()


def slots_of(text: str) -> list[str]:
    """문장에 나오는 자리표시자와 숫자를 등장 순서대로 뽑는다. template_key 와 같은 순서."""
    text = strip_rich(text)
    text = _STORY_VAR.sub("", text)
    return [m.group(0) for m in re.finditer(r"\{[^{}]*\}|\d+(?:\.\d+)?%?", text)]


def numbers_of(text: str) -> list[str]:
    return _NUMBER.findall(unicodedata.normalize("NFKC", text))


def fill_template(zh_template: str, ko_template: str, ocr_text: str) -> str | None:
    """한국어 틀의 {자리표시자}를 화면에서 읽은 숫자로 채운다.

    중국어와 한국어는 어순이 달라 위치로 맞출 수 없다. 그래서 자리표시자 이름으로 찾는다.
    화면의 숫자 개수가 틀과 다르면 잘못 채우느니 None 을 돌려준다.
    """
    slots = slots_of(zh_template)
    values = numbers_of(strip_rich(ocr_text))
    if len(slots) != len(values):
        return None
    table = {}
    fixed = []
    for slot, value in zip(slots, values):
        if slot.startswith("{"):
            table[slot] = value
        elif slot != value:
            fixed.append((slot, value))
    out = _TEMPLATE.sub(lambda m: table.get(m.group(0), m.group(0)), strip_rich(ko_template))
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
