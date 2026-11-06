"""번역 묶음 전체를 기계적으로 훑어 이상한 줄을 찾는다 (사람이 볼 줄을 고르는 용도). 묶음은 고치지 않는다.

  python -I tools/audit_pack.py                 # 항목별 수와 보기
  python -I tools/audit_pack.py --show 반복      # 그 항목의 줄을 다 보기
  python -I tools/audit_pack.py --tsv build/audit.tsv   # 걸린 줄을 '항목<TAB>분류<TAB>원문<TAB>번역' 으로

항목: 빈 번역 · 원문 그대로 · 길이(번역이 원문보다 지나치게 짧거나 김) · 반복(같은 말이 원문보다 많이 되풀이 — 기계
번역의 고리) · 숫자(아라비아 숫자가 다름) · 줄바꿈(\\n 수가 다름) · 덧말(번역기가 붙인 설명: '번역:', '원문:', 'Note') ·
괄호(따옴표·괄호 짝이 원문과 다름) · 한자 남음(괄호 밖) · 자리표('{…}' 가 다름).
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_pack  # noqa: E402

_CJK = re.compile(r"[㐀-鿿]")
_HANGUL = re.compile(r"[가-힣]")
_SLOT = re.compile(r"\{[^{}]*\}")
_NUM = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")
_ZH_NUMERAL = re.compile(r"[〇零一二两三四五六七八九十百千万亿]")
_NOTE = re.compile(r"^\s*(번역\s*[:：]|원문\s*[:：]|Translation|Note\s*:|※\s*번역)|\((?:번역|역주|원문)[^)]*\)", re.I)
_PAIRS = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ("【", "】"), ("《", "》"), ("（", "）"), ("(", ")"), ("[", "]"))
_KO_PAIRS = {"“": "“", "”": "”", "‘": "‘", "’": "’", "「": "‘", "」": "’", "『": "“", "』": "”", "【": "【", "】": "】",
             "《": "<", "》": ">", "（": "(", "）": ")", "(": "(", ")": ")", "[": "[", "]": "]"}


def _repeats(text: str) -> int:
    """가장 많이 되풀이된 3~12자 덩어리의 횟수 (띄어쓰기 무시)."""
    s = re.sub(r"\s+", "", text)
    best = 0
    for size in (3, 4, 6, 8, 12):
        if len(s) < size * 3:
            break
        counts = collections.Counter(s[i:i + size] for i in range(0, len(s) - size + 1))
        if counts:
            best = max(best, counts.most_common(1)[0][1])
    return best


def _cjk_outside_brackets(ko: str) -> bool:
    stripped = re.sub(r"\([^()]*\)|（[^（）]*）", "", ko)
    return bool(_CJK.search(stripped))


def check(zh: str, ko: str) -> list[str]:
    out = []
    if not ko.strip():
        return ["빈 번역"]
    if ko.strip() == zh.strip() and _CJK.search(zh):
        return ["원문 그대로"]
    zn, kn = len(_CJK.findall(zh)), len(_HANGUL.findall(ko))
    if zn >= 8 and (kn < zn * 0.45 or kn > zn * 3.2):
        out.append("길이")
    if _repeats(ko) >= max(4, _repeats(zh) + 3):
        out.append("반복")
    zn_ = sorted(_NUM.findall(_SLOT.sub("", zh)))
    kn_ = sorted(_NUM.findall(re.sub(r"(?<=\d),(?=\d{3})", "", _SLOT.sub("", ko))))   # 20,000 = 20000
    if zn_ != kn_ and not set(zn_) <= set(kn_) and not _ZH_NUMERAL.search(zh):
        out.append("숫자")
    if zh.count("\\n") != ko.count("\\n") and zh.count("\n") != ko.count("\n"):
        out.append("줄바꿈")
    if _NOTE.search(ko) and not _NOTE.search(zh):
        out.append("덧말")
    for a, b in _PAIRS:
        if (zh.count(a) - zh.count(b)) == 0 and a in _KO_PAIRS and ko.count(_KO_PAIRS[a]) != ko.count(_KO_PAIRS[b]) \
                and _KO_PAIRS[a] != _KO_PAIRS[b] and zh.count(a):
            out.append("괄호")
            break
    if _cjk_outside_brackets(ko):
        out.append("한자 남음")
    if sorted(_SLOT.findall(zh)) != sorted(_SLOT.findall(ko)):
        out.append("자리표")
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="번역 묶음 이상한 줄 찾기")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "ak-translations"))
    ap.add_argument("--show", help="이 항목의 줄을 다 보여 준다")
    ap.add_argument("--tsv", help="걸린 줄을 TSV 로 쓴다")
    args = ap.parse_args()
    done = build_pack.load_existing(args.out)
    found = collections.defaultdict(list)
    total = 0
    for cat, rows in done.items():
        for zh, ko in rows.values():
            total += 1
            for kind in check(zh, ko):
                found[kind].append((cat, zh, ko))
    print(f"전체 {total:,}줄")
    for kind, rows in sorted(found.items(), key=lambda kv: -len(kv[1])):
        print(f"  {kind:6} {len(rows):5,}줄 ({len(rows) / total:.2%})")
        for cat, zh, ko in rows[: (len(rows) if args.show == kind else 3)]:
            print(f"      [{cat}] {zh[:60]!r}\n        → {ko[:80]!r}")
    if args.tsv:
        with open(args.tsv, "w", encoding="utf-8") as f:
            for kind, rows in found.items():
                for cat, zh, ko in rows:
                    f.write(f"{kind}\t{cat}\t{zh}\t{ko}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
