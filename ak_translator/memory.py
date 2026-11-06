"""사람이 읽고 고칠 수 있는 Markdown 번역 저장소.

번역캐시.md   한 번 번역한 문장. 프로그램이 뒤에 덧붙이고, 다음 실행 때 그대로 읽는다.
용어집.md     사용자가 직접 정하는 용어. 공식 번역보다 우선한다.

표의 한 줄이 번역 하나다. 파일에서 번역문을 고치고 저장하면 프로그램이 알아서 다시
읽으므로, 마음에 안 드는 번역은 메모장으로 고치면 된다.
"""
from __future__ import annotations

import os
import threading

from . import textnorm as tn

CACHE_HEADER = """# 번역 캐시

한 번 번역한 문장이 여기에 쌓입니다. 다음에 같은 문장이 나오면 번역기를 부르지 않고 바로 씁니다.
번역문을 고치고 저장하면 게임 중에도 바로 반영됩니다.
행을 지우면 그 문장은 다음에 다시 번역합니다.

| 원문 | 번역 | 출처 |
|---|---|---|
"""

GLOSSARY_HEADER = """# 사용자 용어집

공식 번역보다 **먼저** 적용됩니다. 한국 서버에 아직 없는 오퍼레이터·지명의 이름을 정해 두거나,
공식 번역을 바꾸고 싶을 때 씁니다. 저장하면 게임 중에도 바로 반영됩니다.

| 中文 | 한국어 |
|---|---|
"""


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _unescape(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt == "n" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return None
    if "\\" not in line:
        # 이스케이프가 없는 줄이 대부분이다. 미리 번역으로 수만 줄이 되어도 켤 때 바로 읽는다
        return [c.strip() for c in line[1:-1].split("|")]
    cells, cur, i = [], "", 1
    body = line[:-1]
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            cur += body[i:i + 2]
            i += 2
            continue
        if ch == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    cells.append(cur.strip())
    return cells


def read_table(path: str) -> list[list[str]]:
    rows: list[list[str]] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            cells = _cells(line)
            if not cells or len(cells) < 2:
                continue
            if set(cells[0]) <= {"-", ":", " "} or cells[0] in ("원문", "中文"):
                continue
            rows.append([_unescape(c) for c in cells])
    return rows


class MarkdownStore:
    """파일을 메모리 표로 올려 두고, 새 항목은 파일 끝에 덧붙인다.

    파일이 바깥에서 고쳐지면(수정 시각이 바뀌면) 다시 읽는다.
    """

    def __init__(self, path: str, header: str):
        self.path = path
        self.header = header
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._table: dict[str, tuple[str, str]] = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(header)
        self._load()

    def _load(self) -> None:
        table = {}
        for row in read_table(self.path):
            k = tn.key(row[0])
            if k and row[1]:
                table[k] = (row[1], row[2] if len(row) > 2 else "")
        self._table = table
        self._mtime = os.path.getmtime(self.path)

    def refresh(self) -> bool:
        """바깥에서 고쳤으면 다시 읽는다. 바뀌었으면 True."""
        with self._lock:
            try:
                changed = os.path.getmtime(self.path) != self._mtime
                if changed:
                    self._load()
                return changed
            except OSError:
                return False

    def get(self, k: str) -> tuple[str, str] | None:
        return self._table.get(k)

    def items(self) -> list[tuple[str, str]]:
        return [(k, v[0]) for k, v in self._table.items()]

    def __len__(self) -> int:
        return len(self._table)

    def put(self, source: str, text: str, origin: str) -> None:
        k = tn.key(source)
        if not k or not text:
            return
        with self._lock:
            if k in self._table:
                return
            self._table[k] = (text, origin)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"| {_escape(source)} | {_escape(text)} | {origin} |\n")
            self._mtime = os.path.getmtime(self.path)

    def replace(self, entries: list[tuple[str, str]], origin: str, only_from: set[str]) -> int:
        """여러 줄을 한 번에 쓴다. 이미 있는 줄은 그 출처가 only_from 일 때만 그 자리에서 바꾼다.

        먼저 기계 번역으로 띄워 둔 줄을 더 나은 번역으로 갈아 끼우는 데 쓴다. 출처 칸이 기계 번역기
        이름이 아니면 사용자가 손댄 줄로 보고 건드리지 않는다. 바꾼 줄 수를 돌려준다.
        """
        with self._lock:
            self.refresh_unlocked()
            with open(self.path, encoding="utf-8-sig") as f:
                rows = f.read().split("\n")
            where = {}
            for n, line in enumerate(rows):
                cells = _cells(line)
                if cells and len(cells) >= 2:
                    where[tn.key(_unescape(cells[0]))] = n
            changed = 0
            for source, text in entries:
                k = tn.key(source)
                if not k or not text:
                    continue
                old = self._table.get(k)
                if old is not None and old[1] not in only_from:
                    continue
                row = f"| {_escape(source)} | {_escape(text)} | {origin} |"
                if k in where:
                    rows[where[k]] = row
                else:
                    if rows and rows[-1] == "":
                        rows.pop()
                    rows.append(row)
                    rows.append("")
                    where[k] = len(rows) - 2
                self._table[k] = (text, origin)
                changed += 1
            if changed:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(rows))
                os.replace(tmp, self.path)
                self._mtime = os.path.getmtime(self.path)
            return changed

    def refresh_unlocked(self) -> None:
        try:
            if os.path.getmtime(self.path) != self._mtime:
                self._load()
        except OSError:
            pass
