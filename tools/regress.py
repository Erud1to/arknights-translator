"""저장한 문제 화면(debug/*)으로 지금 코드를 다시 돌려 무엇이 달라졌는지 본다. 기계 번역은 부르지 않는다.

  python -I tools/regress.py replay            # 인식.md 의 'OCR 이 읽은 글자' 를 지금 resolver 에 넣고 그때 띄운 번역과 비교
  python -I tools/regress.py pipeline          # 원본 화면을 OCR 부터 다시 읽어 블록이 어디서 번역을 얻는지 센다
  python -I tools/regress.py pipeline --ocr paddle --only 20261104-013114

pipeline 은 결과를 build/regress/pipeline_<ocr>.json 에 쓰고, 지난번 결과가 있으면 달라진 블록만 보여 준다.
AK_DATA 환경 변수로 다른 데이터 폴더(새 DB 를 넣은 사본)를 쓸 수 있다.
"""
import argparse
import collections
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import config  # noqa: E402

OUT_DIR = os.path.join(ROOT, "build", "regress")
MODEL_DIR = ""      # --model-dir: 다른 PaddleOCR 모델 폴더 (그 안에 ppocr/)
TAG = ""            # --tag: 결과 파일 이름 꼬리. 주면 처음에는 꼬리 없는 결과(지금 모델)와 견준다


def load_resolver():
    from ak_translator.resolver import Resolver

    res = Resolver(config.load(), os.environ.get("AK_DATA") or config.DATA_DIR)
    res.enqueue = lambda *a, **k: None
    for _ in range(200):    # 이름 사전·묶음 용어는 뒤에서 만든다
        if res.names.entries and res._pack_terms:
            break
        time.sleep(0.1)
    return res


def captures(only: list[str]) -> list[str]:
    found = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "debug", "*", "인식.md")))
    return [c for c in found if not only or any(c.startswith(o) for o in only)]


def _rows(md: str, title: str) -> list[list[str]]:
    part = md.split(title, 1)
    if len(part) < 2:
        return []
    out = []
    for line in part[1].split("\n## ")[0].splitlines():
        if line.startswith("| ") and not line.startswith(("| ---", "|---")):
            out.append([c.strip() for c in line.strip().strip("|").split(" | ")])
    return out[1:]


def replay(res, caps: list[str]) -> None:
    seen = set()
    changed = same = 0
    for cap in caps:
        with open(os.path.join(ROOT, "debug", cap, "인식.md"), encoding="utf-8") as f:
            md = f.read()
        shown = {r[1]: r[2] for r in _rows(md, "## 화면에 띄운 번역") if len(r) >= 3}
        for r in _rows(md, "## OCR 이 읽은 글자"):
            text = r[0].replace("\\|", "|")
            if text in seen:
                continue
            seen.add(text)
            got = res.resolve(text, allow_mt=False)
            new = got.text or f"<{got.via}>"
            old = shown.get(text, "<안 뜸>")
            if new.replace("\n", " ") != old.replace("<br>", " "):
                changed += 1
                print(f"{cap} {text[:40]!r}\n    전: {old[:60]}\n    후: {got.via} {new[:60]} trim={got.trim}")
            else:
                same += 1
    print(f"같음 {same} · 달라짐 {changed}")


def _engine(name: str, res):
    from ak_translator import ppocr
    from ak_translator.ocr import WindowsOCR

    if name == "windows":
        return WindowsOCR()
    meta = res.db.meta
    ocr = ppocr.PaddleOCR(MODEL_DIR or config.MODEL_DIR, meta.get("charset", ""), rare=meta.get("rare", ""))
    ocr.warm(2560, 1440)
    return ocr


def pipeline(res, caps: list[str], engines: list[str]) -> None:
    from PIL import Image

    from ak_translator import recruit, textnorm as tn
    from ak_translator.engine import _picker, frame_blocks

    rec = recruit.Recruiter(res.db, lambda zh: zh)
    os.makedirs(OUT_DIR, exist_ok=True)
    for name in engines:
        ocr = _engine(name, res)
        path = os.path.join(OUT_DIR, f"pipeline_{name}{'_' + TAG if TAG else ''}.json")
        base = path if os.path.exists(path) or not TAG else os.path.join(OUT_DIR, f"pipeline_{name}.json")
        before = {}
        if os.path.exists(base):
            with open(base, encoding="utf-8") as f:
                before = json.load(f)
        out = dict(before)
        blocks_by, chars_by = collections.Counter(), collections.Counter()
        diffs = 0
        for cap in caps:
            png = os.path.join(ROOT, "debug", cap, "1_원본.png")
            if not os.path.exists(png):
                continue
            frame = Image.open(png).convert("RGB")
            rows = []
            for b in frame_blocks(ocr, frame, res, rec):
                r = res.resolve(b.text, allow_mt=False, masked=b.masked, pick=_picker(frame, b))
                via = r.via if r.text or r.via == "skip" else "pending"
                blocks_by[via] += 1
                chars_by[via] += tn.cjk_count(b.text)
                rows.append([via, b.text, r.text or ""])
            out[cap] = rows
            if cap in before:
                old, new = {tuple(r) for r in before[cap]}, {tuple(r) for r in rows}
                for via, text, ko in sorted(old - new, key=lambda r: r[1]):
                    print(f"{cap} - [{via}] {text[:40]!r} → {ko[:40]!r}")
                for via, text, ko in sorted(new - old, key=lambda r: r[1]):
                    print(f"{cap} + [{via}] {text[:40]!r} → {ko[:40]!r}")
                diffs += len(old ^ new)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"{name}: 블록 {dict(blocks_by)}")
        print(f"{name}: 한자 {dict(chars_by)}")
        print(f"{name}: 지난번과 다른 블록 {diffs}" if before else f"{name}: 처음 돌림 — 다음부터 비교한다 ({path})")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="문제 화면 회귀 시험")
    ap.add_argument("mode", choices=("replay", "pipeline"))
    ap.add_argument("--ocr", choices=("paddle", "windows", "both"), default="paddle")
    ap.add_argument("--only", nargs="*", default=[], help="이 이름으로 시작하는 캡처만")
    ap.add_argument("--model-dir", default="", help="pipeline: 다른 PaddleOCR 모델 폴더")
    ap.add_argument("--tag", default="", help="pipeline: 결과 파일 이름 꼬리 (다른 모델을 견줄 때)")
    args = ap.parse_args()
    global MODEL_DIR, TAG
    MODEL_DIR, TAG = args.model_dir, args.tag
    res = load_resolver()
    caps = captures(args.only)
    try:
        if args.mode == "replay":
            replay(res, caps)
        else:
            pipeline(res, caps, ["paddle", "windows"] if args.ocr == "both" else [args.ocr])
    finally:
        res.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
