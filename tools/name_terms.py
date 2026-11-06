"""공식 번역에서 늘 같은 한국어로 옮겨지는 고유 이름 (이벤트·작전·스테이지·건물·통합전략 이름 등).

용어 표(terms)의 이름 가운데 번역기 용어집(glossary.KIND_MIN_LEN)이 지키지 않는 표의 것('视相博物馆' = activity_table)은
묶음을 만들 때 기계 번역이 제멋대로 옮겼다 ('시상 박물관', 공식은 '폴리비전 박물관'). 그런 표에는 '知道了'(알겠다),
'第一次'(최초) 같은 보통 말도 이름으로 들어 있어 통째로 지킬 수는 없다. 그래서 공식 문장에서 확인한다:
그 중국어가 든 공식 문장 쌍에서 한국어 이름이 FIXED_RATIO 넘게 함께 나오면 고유 이름이다.

  fixed_names(db_path) -> {중국어: 한국어}   (결과는 build/name_terms_<DB 만든 날>.json 에 담아 둔다)
  python -I tools/name_terms.py              # 몇 개인지와 보기
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import textnorm as tn  # noqa: E402

FIXED_RATIO = 0.7       # 공식 문장에서 이름 번역이 함께 나오는 비율 하한
MIN_SEEN = 3            # 공식 문장에 이만큼은 나와야 판단한다 (적으면 보통 말인지 알 수 없다)
MANY_SEEN = 10          # 한 표에서만 나오는 이름은 이만큼은 나와야 믿는다 ('新航线' 은 스킨 설명에만 '뉴 루츠' 로 나오는
                        # 스킨 시리즈 이름이지만, 묶음 대사에서는 그냥 '새 항로' 였다)
SAMPLE = 200            # 이름마다 볼 공식 문장 수 상한
_CJK = re.compile(r"[㐀-鿿]")
_NORM = re.compile(r"[\s'‘’\"“”·\-–—:：,，.。!！?？()（）\[\]【】<>《》『』「」]")


def norm(text: str) -> str:
    return _NORM.sub("", text).lower()


def candidates(con) -> dict[str, tuple[str, str]]:
    out = {}
    for zh, ko, kind, _n in con.execute("SELECT zh, ko, kind, n FROM terms"):
        cjk = len(_CJK.findall(zh))
        if kind == "speaker" or not ko or cjk < 3 or cjk < len(zh) * 0.8 or len(zh) > 16:
            continue
        ko = tn.strip_rich(ko).strip()
        if ko and not _CJK.search(ko) and "((" not in ko:
            out[zh] = (ko, kind)
    return out


def consistency(con, zh: str, ko: str) -> tuple[int, int, int]:
    """(그 중국어가 든 공식 문장 수, 그중 한국어 이름도 든 수, 그 문장들이 나온 표 수). 이름 자신의 줄은 뺀다."""
    grams = {zh[i:i + 3] for i in range(len(zh) - 2)}
    query = " AND ".join(f'"{g}"' for g in sorted(grams) if '"' not in g)
    try:
        rows = con.execute(
            "SELECT t.zh, t.ko, t.src FROM fts JOIN texts t ON t.id = fts.rowid WHERE fts MATCH ? AND t.ko IS NOT NULL "
            "LIMIT ?", (query, SAMPLE * 3)).fetchall()
    except sqlite3.Error:
        return 0, 0, 0
    want = norm(ko)
    seen = hit = 0
    srcs = set()
    for z, k, src in rows:
        if zh not in z or z == zh:
            continue
        seen += 1
        hit += want in norm(tn.strip_rich(k))
        srcs.add(src)
        if seen >= SAMPLE:
            break
    return seen, hit, len(srcs)


def fixed_names(db_path: str) -> dict[str, str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        built = dict(con.execute("SELECT name, value FROM meta").fetchall()).get("built_at", "unknown")
        cache = os.path.join(ROOT, "build", f"name_terms_v2_{re.sub(r'[^0-9]', '', built)}.json")
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        out = {}
        for zh, (ko, _kind) in candidates(con).items():
            seen, hit, srcs = consistency(con, zh, ko)
            if seen >= MIN_SEEN and hit >= seen * FIXED_RATIO and (srcs >= 2 or seen >= MANY_SEEN):
                out[zh] = ko
    finally:
        con.close()
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    import time

    t = time.time()
    names = fixed_names(os.path.join(ROOT, "data", "official.sqlite"))
    print(f"공식 문장에서 확인된 고유 이름 {len(names):,}개 ({time.time() - t:.0f}초)")
    for zh in list(names)[:20]:
        print(f"  {zh} = {names[zh]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
