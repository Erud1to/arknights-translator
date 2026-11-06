"""번역 묶음의 줄을 손으로 고친다. 묶음에 있는 원문만 고치고, manifest 의 '직접 고침' 수에 더해 다시 쓴다.

  python -I tools/hand_fix.py "搬两个小桶=작은 통 두 개를 옮긴다" "获得源流样本！=원천 샘플 획득!"
  python -I tools/hand_fix.py --file 고칠줄.tsv          # 한 줄에 '원문<TAB>번역'
  python -I tools/hand_fix.py --dry-run ...               # 쓰지 않고 바뀔 것만 본다
  python -I tools/hand_fix.py --category templates --file 새줄.tsv   # 묶음에 없는 원문은 이 분류에 새로 넣는다

고친 뒤 tools/install_pack.py 로 이 프로그램에 넣는다. 한 PC 에서만 쓸 고침은 묶음 대신 데이터 탭의 용어집에 둔다.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_pack  # noqa: E402
from ak_translator import pack, textnorm as tn  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="묶음 줄 손으로 고치기")
    ap.add_argument("pairs", nargs="*", help="'원문=번역'")
    ap.add_argument("--file", help="원문<TAB>번역 줄이 든 파일")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "ak-translations"), help="번역 저장소 폴더")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--category", help="묶음에 없는 원문을 새로 넣을 분류 (예: templates). 없으면 건너뛴다")
    args = ap.parse_args()
    fixes = {}
    for pair in args.pairs:
        zh, sep, ko = pair.partition("=")
        if not sep:
            raise SystemExit(f"'원문=번역' 꼴이 아니다: {pair}")
        fixes[zh.strip()] = ko.strip()
    if args.file:
        for zh, ko in pack.read_tsv(args.file):
            fixes[zh] = ko
    if not fixes:
        raise SystemExit(__doc__)
    done = build_pack.load_existing(args.out)
    fixed, missing = 0, []
    for zh, ko in fixes.items():
        k = tn.key(zh)
        cats = [cat for cat, rows in done.items() if k in rows]
        if not cats and args.category:
            print(f"  [{args.category}] 새 줄 {zh} → {ko}")
            done.setdefault(args.category, {})[k] = (zh, ko)
            fixed += 1
            continue
        if not cats:
            missing.append(zh)
            continue
        for cat in cats:
            print(f"  [{cat}] {zh} | {done[cat][k][1]} → {ko}")
            done[cat][k] = (done[cat][k][0], ko)
            fixed += 1
    for zh in missing:
        print(f"  묶음에 없는 원문 (건너뜀): {zh}")
    if args.dry_run or not fixed:
        print(f"고칠 줄 {fixed}" + (" (쓰지 않음)" if args.dry_run else ""))
        return 1 if missing and not fixed else 0
    with open(os.path.join(args.out, pack.MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    engines = dict(manifest.get("engines", {}))
    engines["직접 고침"] = engines.get("직접 고침", 0) + fixed
    m = build_pack.write(args.out, done, manifest.get("game_revision", ""), engines)
    print(f"고친 줄 {fixed} · 판 {m['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
