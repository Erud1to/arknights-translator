"""번역캐시(data/번역캐시.md) 정리. --apply 가 없으면 지울 줄만 보여 준다. 번역기를 끈 뒤에 쓴다.

  python -I tools/clean_cache.py
  python -I tools/clean_cache.py --apply
  python -I tools/clean_cache.py --replace 피빛=핏빛 --apply     # 번역문의 낱말을 바꾼다 (여러 번 줄 수 있다)

지우는 줄:
  - 게임 글이 아닌(OCR 이 읽은) 원문인데 지금 코드로는 번역캐시까지 오지 않는 줄 (헛것 '一“011零件箱', 반만 읽은 '标生命',
    이름을 잘못 읽은 '罗比塔'). 다시 쓰일 일이 없는 엉뚱한 번역이다
  - 번역문에 통합전략 공식 이름·묶음 용어가 빠진 줄 ('林间夜话' → '숲속의 밤 이야기'). 다음에 볼 때 다시 번역한다
지우기 전 파일은 build/backup 에 둔다.
"""
import argparse
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import config, memory  # noqa: E402
from ak_translator.resolver import Resolver  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="번역캐시 정리")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--replace", action="append", default=[], metavar="예전=새것")
    args = ap.parse_args()
    swaps = [tuple(s.split("=", 1)) for s in args.replace if "=" in s]
    path = os.path.join(config.DATA_DIR, "번역캐시.md")
    res = Resolver(config.load(), config.DATA_DIR)
    res.enqueue = lambda *a, **k: None
    for _ in range(200):
        if res.names.entries and res._pack_terms:
            break
        time.sleep(0.1)
    terms = dict(res.db.rogue_names())
    terms.update({zh: res.glossary.base[zh] for zh in res._pack_terms if zh in res.glossary.base})

    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    out, dropped, swapped = [], [], 0
    for line in lines:
        cells = memory._cells(line)
        if not cells or len(cells) < 2 or set(cells[0]) <= {"-", ":", " "} or cells[0] in ("원문", "中文"):
            out.append(line)
            continue
        src, ko = memory._unescape(cells[0]), memory._unescape(cells[1])
        reason = ""
        if res.db.lookup(src, fuzzy=False) is None:
            got = res.resolve(src, allow_mt=False)
            if got.via != "cache":
                reason = f"이제 {got.via}" + (f" '{got.text}'" if got.text else "")
        if not reason:
            miss = [zh for zh, name in terms.items() if zh in src and name not in ko]
            if miss:
                reason = "공식 이름 빠짐 " + ", ".join(f"{zh}={terms[zh]}" for zh in miss)
        if reason:
            dropped.append((src, ko, reason))
            continue
        for old, new in swaps:
            if old in ko:
                line = line.replace(memory._escape(ko), memory._escape(ko.replace(old, new)), 1)
                ko = ko.replace(old, new)
                swapped += 1
        out.append(line)
    res.stop()
    for src, ko, reason in dropped:
        print(f"- {src[:40]!r} → {ko[:30]!r}  ({reason})")
    kept = sum(1 for l in out if l.startswith("| ")) - 1
    print(f"지울 줄 {len(dropped)} · 낱말 바꿀 줄 {swapped} · 남는 줄 {kept}")
    if args.apply and (dropped or swapped):
        backup = os.path.join(ROOT, "build", "backup")
        os.makedirs(backup, exist_ok=True)
        copy = os.path.join(backup, time.strftime("번역캐시_%Y%m%d-%H%M%S.md"))
        shutil.copy2(path, copy)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.writelines(out)
        print("적용함. 예전 파일:", copy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
