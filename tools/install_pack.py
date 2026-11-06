"""번역 묶음(번역 저장소)을 검사하고 이 프로그램의 data/pack 에 넣는다.

  python -I tools/install_pack.py            # 검사 + 설치
  python -I tools/install_pack.py --stage    # + 번역 저장소에서 git add (커밋은 하지 않는다)
  python -I tools/install_pack.py --check    # 검사만

검사하는 것: manifest 의 sha256, 번역기 자리표(XZ..Q)·숫자 표식(9001…)이 남은 줄, 괄호 밖에 중국어가 남은 줄,
숫자 틀(templates)의 '{…}' 자리표시자가 원문과 번역문에서 같은지. 하나라도 걸리면 설치하지 않는다.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_pack  # noqa: E402
from ak_translator import config, pack  # noqa: E402

_TOKEN = re.compile(r"XZ[A-Z]{1,2}Q", re.I)
_MARK = re.compile(r"(?<!\d)9\d{3}(?!\d)")
_SLOT = re.compile(r"\{[^{}]+\}")


def check(folder: str) -> tuple[dict, list[str]]:
    with open(os.path.join(folder, pack.MANIFEST), encoding="utf-8") as f:
        manifest = json.load(f)
    problems = []
    for name, info in manifest["files"].items():
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            problems.append(f"{name}: 파일 없음")
            continue
        if pack.sha256(path) != info["sha256"]:
            problems.append(f"{name}: sha256 이 manifest 와 다름 (번역 저장소에서 python tools/refresh_manifest.py . 로 고친다)")
        rows = pack.read_tsv(path)
        for zh, ko in rows:
            if _TOKEN.search(ko):
                problems.append(f"{name}: 자리표가 남음 — {zh[:30]}")
            elif name == "templates.tsv" and (_MARK.search(ko) or sorted(_SLOT.findall(zh)) != sorted(_SLOT.findall(ko))):
                problems.append(f"{name}: 숫자 자리 안 맞음 — {zh[:30]}")
            elif not build_pack.usable_translation(ko) and "<御4>" not in zh:
                problems.append(f"{name}: 덜 번역됨 — {zh[:30]} → {ko[:30]}")
        print(f"  {name:16} {len(rows):7,}줄")
    return manifest, problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="번역 묶음 검사·설치")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "ak-translations"), help="번역 저장소 폴더")
    ap.add_argument("--stage", action="store_true", help="번역 저장소에서 git add")
    ap.add_argument("--check", action="store_true", help="검사만 한다")
    args = ap.parse_args()
    manifest, problems = check(args.out)
    print(f"판 {manifest['version']} · 문제 {len(problems)}")
    for line in problems[:30]:
        print("  -", line)
    if problems:
        return 1
    if args.check:
        return 0
    dest = os.path.join(config.DATA_DIR, "pack")
    os.makedirs(dest, exist_ok=True)
    for name in [pack.MANIFEST, *manifest["files"]]:
        shutil.copy2(os.path.join(args.out, name), dest)
    print(f"설치함: {dest} (번역기를 다시 켜거나 데이터 탭에서 묶음을 다시 읽으면 쓴다)")
    if args.stage:
        subprocess.run(["git", "-C", args.out, "add", "-A"], check=False)
        print("번역 저장소에 스테이징함 (커밋은 하지 않음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
