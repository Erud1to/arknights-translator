"""manifest.json 다시 쓰기. TSV 를 손으로(또는 GitHub 웹·Pull Request 로) 고친 뒤 돌린다.

프로그램은 manifest 의 sha256 과 받은 파일이 다르면 받기를 거부한다 (깨진 파일 막기). 그래서 TSV 를 고치면
manifest 도 같이 바꿔야 한다. 이 저장소의 GitHub Action(.github/workflows/manifest.yml)이 TSV 가 바뀔 때마다 자동으로
돌린다. 손으로 돌릴 때:  python tools/refresh_manifest.py .

파이썬 표준 라이브러리만 쓴다 (번역 프로그램 없이 돈다). 파일 내용은 바꾸지 않고, 형식이 깨진 줄이 있으면 알리고 멈춘다.
"""
import datetime as dt
import hashlib
import json
import os
import sys

MANIFEST = "manifest.json"


def check(path: str) -> tuple[int, list[str]]:
    """(번역 줄 수, 문제 목록). 한 줄은 '원문<TAB>번역문', '#' 로 시작하면 설명이다."""
    count, problems = 0, []
    with open(path, encoding="utf-8-sig", newline="\n") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            if "\t" not in line:
                problems.append(f"{os.path.basename(path)}:{n} 탭이 없다 (원문<TAB>번역문)")
                continue
            zh, ko = line.split("\t", 1)
            if not zh.strip() or not ko.strip():
                problems.append(f"{os.path.basename(path)}:{n} 원문이나 번역문이 비었다")
                continue
            count += 1
    return count, problems


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(folder, MANIFEST)
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    files = manifest.setdefault("files", {})
    names = sorted(set(files) | {n for n in os.listdir(folder) if n.endswith(".tsv")})
    problems, changed = [], []
    for name in names:
        full = os.path.join(folder, name)
        if not os.path.exists(full):
            files.pop(name, None)
            changed.append(f"{name} 지움")
            continue
        count, bad = check(full)
        problems += bad
        entry = files.setdefault(name, {"about": name[:-4]})
        digest = sha256(full)
        if entry.get("sha256") != digest or entry.get("count") != count:
            changed.append(f"{name} {entry.get('count', 0):,} → {count:,}줄")
            entry.update(count=count, sha256=digest)
    if problems:
        print("형식이 깨진 줄이 있어 manifest 를 바꾸지 않았습니다:")
        print("\n".join(problems[:50]))
        return 1
    if not changed:
        print("바뀐 파일이 없습니다.")
        return 0
    manifest["version"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y.%m.%d-%H%M")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"manifest 판 {manifest['version']}: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
