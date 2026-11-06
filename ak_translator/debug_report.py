"""문제 화면 기록. 번역이 이상할 때 OCR·문장 묶기·번역 중 어디서 틀렸는지 짚으려고 남긴다.

debug/날짜-시각/
  1_원본.png              오버레이 없는 게임 화면
  2_번역.png              오버레이가 얹힌 화면 (오버레이가 떠 있을 때만)
  3_마지막으로_읽은_화면.png  OCR 이 실제로 읽은 프레임 (화면이 그 뒤로 바뀌었을 수 있다)
  인식.md                 읽은 글자, 묶은 결과, 띄운 번역과 출처, 스토리 위치
"""
from __future__ import annotations

import os
import time

from . import errlog
from .version import VERSION

VIA_NAMES = {"official": "공식", "pack": "번역 묶음", "user": "내 용어", "cache": "저장된 기계 번역"}


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def write(root: str, snap: dict, original, translated, resolver, cfg: dict, recent: list) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(root, "debug", stamp)
    os.makedirs(folder, exist_ok=True)
    original.save(os.path.join(folder, "1_원본.png"))
    if translated is not None:
        translated.save(os.path.join(folder, "2_번역.png"))
    if snap.get("frame") is not None:
        snap["frame"].save(os.path.join(folder, "3_마지막으로_읽은_화면.png"))

    path, idx = snap["story"]
    lines = [
        f"# 문제 화면 기록 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 프로그램: {VERSION}",
        "",
        f"- 게임 영역: {snap['region']}",
        f"- 전투 중으로 판단: {'예' if snap['battle'] else '아니오'}",
        f"- 짧은 대사 판단: {snap.get('short_note') or '-'}",
        f"- 마지막 OCR 시간: {snap['ocr_ms']}ms ({snap.get('ocr_name') or 'OCR'})"
        + (f" · {snap['ocr_note']}" if snap.get("ocr_note") else ""),
        f"- 기계 번역기: {resolver.engine.label} · 표시 방식: {cfg.get('box_style')} · 글자 크기: {cfg.get('font_scale')}",
        f"- 공식 데이터: {resolver.db.meta.get('built_at', '?')}",
    ]
    if path:
        current = resolver.db.current_line()
        nxt = resolver.db.next_line()
        lines += [
            f"- 스토리 위치: `{path}` {idx}번째 줄",
            f"  - 지금 줄: {_cell(current[2]) if current else '-'} → {_cell(current[3]) if current and current[3] else '(공식 번역 없음)'}",
            f"  - 다음 줄: {_cell(nxt[2]) if nxt else '-'}",
        ]
    else:
        lines.append("- 스토리 위치: 따라가는 스토리 없음")

    lines += ["", "## 화면에 띄운 번역", "", "| 출처 | 원문 | 번역 | 위치 (x, y, 폭, 높이) |", "|---|---|---|---|"]
    for via, zh, ko, box in snap["shown"]:
        lines.append(f"| {VIA_NAMES.get(via, via)} | {_cell(zh)} | {_cell(ko)} | {box} |")
    if not snap["shown"]:
        lines.append("| - | (없음) | | |")

    lines += ["", "## OCR 이 읽은 글자 (묶은 단위)", "",
              "번역이 안 뜬 글자는 공식 번역·번역 묶음이 없고 기계 번역을 기다리는 중이거나, 잘못 읽어 걸러진 것이다.", "",
              "| 읽은 글자 | 위치 | 줄 수 |", "|---|---|---|"]
    for text, box, rows in snap["blocks"]:
        lines.append(f"| {_cell(text)} | {box} | {rows} |")
    if not snap["blocks"]:
        lines.append("| (없음) | | |")

    lines += ["", "## 최근 번역 (오래된 것부터)", ""]
    for via, src, ko, *_zh in recent[-15:]:
        lines.append(f"- {VIA_NAMES.get(via, via)}: {_cell(src)} → {_cell(ko)}")

    errors = errlog.tail(3000).strip()
    if errors:
        lines += ["", "## 오류 기록 (끝부분, data/오류기록.txt)", "", "```", errors, "```"]
    with open(os.path.join(folder, "인식.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return folder
