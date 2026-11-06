"""스토리 스크립트(.txt)에서 화면에 보이는 문장만 뽑아낸다.

[name="아미야"]  대사          화자가 있는 대사
[multiline(name="아미야")]대사  한 대사창에 이어서 찍히는 조각
대사만 있는 줄                  지문
[Decision(options="가;나", ...)] 선택지
[Subtitle(text="...")]         자막
[Sticker(text="...")]          화면 글자
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = re.compile(r'^\[name\s*=\s*"([^"]*)"[^\]]*\]\s*(.*)$', re.I)
_MULTI = re.compile(r'^\[multiline\((.*?)\)\]\s*(.*)$', re.I)
_COMMAND = re.compile(r"^\[\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(?:\((.*)\))?\s*\]\s*(.*)$", re.S)
_ATTR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')

TEXT_COMMANDS = {"subtitle": "text", "sticker": "text", "animtext": "text"}


@dataclass
class Line:
    kind: str      # say / option / caption
    speaker: str
    text: str


def attrs(body: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _ATTR.findall(body or "")}


def parse(script: str) -> list[Line]:
    """문장을 등장 순서대로. 이어지는 multiline 조각은 한 줄로 합친다."""
    out: list[Line] = []
    multi: Line | None = None

    def flush() -> None:
        nonlocal multi
        if multi is not None and multi.text:
            out.append(multi)
        multi = None

    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue

        m = _MULTI.match(line)
        if m:
            meta = attrs(m.group(1))
            name = meta.get("name", "")
            if multi is not None and multi.speaker != name:
                flush()
            if multi is None:
                multi = Line("say", name, "")
            multi.text += m.group(2)
            if "end" in m.group(1).lower():
                flush()
            continue
        flush()

        m = _NAME.match(line)
        if m:
            if m.group(2):
                out.append(Line("say", m.group(1), m.group(2)))
            continue

        if line.startswith("["):
            m = _COMMAND.match(line)
            if not m:
                continue
            cmd = m.group(1).lower()
            meta = attrs(m.group(2))
            if cmd == "decision":
                for opt in meta.get("options", "").split(";"):
                    if opt.strip():
                        out.append(Line("option", "", opt.strip()))
            elif cmd in TEXT_COMMANDS and meta.get(TEXT_COMMANDS[cmd]):
                out.append(Line("caption", "", meta[TEXT_COMMANDS[cmd]]))
            continue

        out.append(Line("say", "", line))
    flush()
    return out
