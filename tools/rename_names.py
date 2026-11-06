"""한국 서버에 없는 오퍼레이터 이름을 영문 이름(character_table appellation) 기준 한국 서버 표기로 바로잡는다.

  python -I tools/rename_names.py              # 바꿀 것만 보여 준다
  python -I tools/rename_names.py --apply      # 묶음·번역캐시에 쓴다 (번역캐시 사본은 build/backup 에)
  python -I tools/rename_names.py --find 冻河   # 그 이름이 묶음 번역문에서 어떻게 옮겨졌는지 후보를 뽑는다

1. 번역 묶음에 names.tsv 를 쓴다 (화면 이름표는 이것을 먼저 쓴다).
2. 그 이름이 든 묶음 줄·번역캐시 줄에서 예전 기계 번역 표기(변형 여럿)를 새 이름으로 바꾸고 뒤 조사를 받침에 맞춘다.
   낱말 경계에서만, 그 원문에 중국어 이름이 든 줄에서만 바꾼다 ('동하' 가 '이동하여' 안에서 바뀌지 않게).
새 이름을 더할 때: --find 로 예전 표기 후보를 보고 NAMES 에 (새 이름, [예전 표기 — 긴 것부터], 영문 이름) 을 더한다.
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_pack  # noqa: E402
from ak_translator import config, memory, pack, textnorm as tn  # noqa: E402
from ak_translator.glossary import _fix_particle  # noqa: E402

# 중국어 이름: (새 이름, [예전 표기 — 긴 것부터], 영문 이름)
NAMES = {
    "嘉辛塔": ("자신타", ["가시엔타", "카신타", "가신타", "자시타"], "Jacinta"),
    "岳羽由加莉": ("타케바 유카리", ["가쿠하 유카리", "야쿠 유카리", "가쿠 유카리", "후카 유카리"], "Yukari Takeba"),
    "珊比": ("썸피", ["샨비", "산비", "샌비"], "Thumpy"),
    "予愿安洁莉娜": ("안젤리나 더 멜로우 위시", ["나의 소원 안젤리나", "유원 안젤리나", "소원 안젤리나"],
               "Angelina the Mellow Wish"),
    "德·托莱多": ("데 톨레도", ["데·톨레도", "데 토레도", "드 톨레도", "토레도"], "de Toledo"),
    "罗德岛隐秘队": ("로도스 아일랜드 정찰대", ["로도스 아일랜드 비밀 부대", "로도스 아일랜드 은밀한 팀", "로도스 아일랜드 은밀대"],
                "Rhodes Island Recon"),
    "镜匠": ("칼레이도", ["거울 장인", "안경 장인", "안경사"], "Caleido"),
    "冻河": ("토스카", ["얼어붙은 강", "동결된 강", "동하", "동허", "동강"], "Тоска"),
    "旅骨": ("홈바운드", ["여행의 흔적", "여행자 뼈", "리부 골", "류골", "류구"], "Homebound"),
    "谬因": ("아프리사", ["오류의 원인", "미우인", "뮤인", "미인"], "Aphrissa"),
    "结城理": ("유키 마코토", ["결성 리", "유키 리"], "Makoto Yuki"),
    "焰狐龙梓兰": ("바이올렛 타마미츠네 오키드", ["화염여우용 오키드", "화염여우룡 오키드", "유호룡 오키드"],
              "Violet Mizutsune Orchid"),
    "行铃": ("허드벨스", ["행링", "행령"], "Herdbells"),
    "埃癸斯": ("아이기스", ["에기스", "아기스", "이지스"], "Aegis"),
    "虎狼丸": ("코로마루", ["호랑이와 늑대마루", "호랑이 늑대마루", "호랑이·늑대환", "호랑이늑대환", "호랑이늑대알",
                       "늑대마루", "고로마루"], "Koromaru"),
    "国度": ("테리토리야", [], "Территория"),      # 문장에서는 '그의 국도(나라)' 말장난이라 정한 자리만 (아래 PINNED)
    "雷狼龙S空爆": ("진오우거S 캐터펄트", ["레이랑룡 S 캐터펄트", "뇌랑룡 S 캐터펄트"], "Zinogre S Catapult"),
    "时隙": ("타임슬롯", ["타임슬릿", "시쉐", "시갭", "시시"], "Timeslot"),
    "佩德洛": ("페드로", ["페드"], "Pedro"),
    # 사용자 지정 (2026-11-06). 통합전략 NPC 로도 나오지만 같은 인물이다. '화이트스미스' 는 다른 사람(스승)이라 두지 않는다
    "机械师": ("메커니스트", ["기계술사", "기계공", "기계사"], "Mechanist"),
}
# 이름 자리가 분명한 꼴만 바꾸는 이름: 원문 조각 -> (예전 번역 조각, 새 번역 조각)
PINNED = {
    "国度": [("【代号】国度", "【코드명】국가", "【코드명】테리토리야"),
             ("非助战国度", "비지원 국가", "비지원 테리토리야")],
}
JOSA = r"(이라고|라고|에게서|에게|에서|으로|이랑|이나|까지|부터|처럼|보다|의|은|는|이|가|을|를|과|와|도|만|로|랑)?"


def patterns(variants):
    return [(v, re.compile(r"(?<![가-힣])" + re.escape(v) + JOSA + r"(?![가-힣])")) for v in variants]


COMPILED = {zh: patterns(v) for zh, (_new, v, _en) in NAMES.items()}


def rename(zh: str, ko: str) -> str:
    out = ko
    for name, (new, _variants, _en) in NAMES.items():
        if name not in zh:
            continue
        for original, old, fixed in PINNED.get(name, []):
            if original in zh:
                out = out.replace(old, fixed)
        for _v, pattern in COMPILED[name]:
            def repl(m, new=new):
                josa = m.group(1) or ""
                if m.group(0) == new or new.startswith(m.group(0)):
                    return m.group(0)       # 이미 새 이름 ('페드'+'로' = '페드로')
                if not josa:
                    return new
                following = m.string[m.end():m.end() + 1]
                return new + _fix_particle(new, josa, following)
            out = pattern.sub(repl, out)
    return out


def find_variants(done: dict, names: list[str]) -> None:
    """그 이름이 든 줄에서 자주 나오고 다른 줄에서는 드문 낱말·낱말 묶음 (사람이 골라 NAMES 에 넣는다)."""
    josa = re.compile(JOSA.strip("?") + "$")
    rows = [(z, k) for c in done.values() for z, k in c.values()]

    def grams(ko: str) -> set[str]:
        words = [w for w in (josa.sub("", w) for w in re.findall(r"[가-힣A-Za-z·\-]+", ko)) if w]
        out = set(words)
        for n in (2, 3):
            out |= {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}
        return out

    background = collections.Counter()
    for _z, k in rows:
        background.update(grams(k))
    for name in names:
        mine = collections.Counter()
        hit = [k for z, k in rows if name in z]
        for k in hit:
            mine.update(grams(k))
        scored = sorted(((n / (1 + background[g] - n), n, g) for g, n in mine.items() if n >= 2 or len(hit) <= 3),
                        reverse=True)
        print(f"== {name} ({len(hit)}줄):", ", ".join(f"{g}({n}/{background[g]})" for _s, n, g in scored[:14]))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="한국 서버에 없는 오퍼레이터 이름 바로잡기")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--find", nargs="+", metavar="중국어이름")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "ak-translations"), help="번역 저장소 폴더")
    args = ap.parse_args()
    done = build_pack.load_existing(args.out)
    if args.find:
        find_variants(done, args.find)
        return 0
    changed, samples = 0, []
    for rows in done.values():
        for k, (zh, ko) in list(rows.items()):
            new = rename(zh, ko)
            if new != ko:
                changed += 1
                rows[k] = (zh, new)
                if len(samples) < 25:
                    samples.append((ko[:70], new[:70]))
    done["names"] = {tn.key(zh): (zh, new) for zh, (new, _v, _en) in NAMES.items()}
    for old, new in samples:
        print(f"  - {old}\n    + {new}")
    print(f"묶음에서 바꿀 줄 {changed:,} · names.tsv {len(done['names'])}줄")

    cache_path = os.path.join(config.DATA_DIR, "번역캐시.md")
    with open(cache_path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    out, cache_changed = [], 0
    for line in lines:
        cells = memory._cells(line)
        if cells and len(cells) >= 2 and not set(cells[0]) <= {"-", ":", " "} and cells[0] not in ("원문", "中文"):
            src, ko = memory._unescape(cells[0]), memory._unescape(cells[1])
            new = rename(src, ko)
            if new != ko:
                line = line.replace(memory._escape(ko), memory._escape(new), 1)
                cache_changed += 1
        out.append(line)
    print(f"번역캐시에서 바꿀 줄 {cache_changed:,}")
    if not args.apply:
        return 0
    with open(os.path.join(args.out, pack.MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    engines = dict(manifest.get("engines", {}))
    engines["직접 고침"] = engines.get("직접 고침", 0) + len(done["names"])
    m = build_pack.write(args.out, done, manifest.get("game_revision", ""), engines)
    backup = os.path.join(ROOT, "build", "backup")
    os.makedirs(backup, exist_ok=True)
    shutil.copy2(cache_path, os.path.join(backup, time.strftime("번역캐시_%Y%m%d-%H%M%S.md")))
    with open(cache_path, "w", encoding="utf-8", newline="") as f:
        f.writelines(out)
    print("적용함. 판", m["version"], "(번역기가 켜져 있으면 다시 켜야 번역캐시를 다시 읽는다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
