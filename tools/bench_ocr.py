"""Windows OCR(엔진처럼 넷으로 나눠 다시 읽기까지)과 PaddleOCR 을 저장한 문제 화면 전부에서 비교한다.

  python -I tools/bench_ocr.py
  python -I tools/bench_ocr.py --cpu 4      # PaddleOCR 을 CPU 로도 앞 4장 재 본다

지표
  - 확인된 한자: 한자 2자 이상인 줄 가운데 게임 데이터(공식 DB 글·숫자 틀)에 그대로 있는 줄의 한자 수.
    잘못 읽은 줄은 DB 에 없으니 클수록 바르게 읽은 글이 많다. 확인 안 된 한자에는 플레이어 이름처럼 원래 없는 글도 섞인다.
  - 체크리스트: 지금까지 문제 화면에서 틀렸던 자리에 있어야 할 글이 몇 번 나오는가.
  - 속도: 한 장당 초 (첫 장 제외).
결과: build/regress/bench_ocr.json
"""
import argparse
import glob
import json
import os
import sys
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import config  # noqa: E402

CHECKS = {
    "20261105-201903": [("治疗", 1)],
    "20261105-201928": [("治疗", 1)],
    "20261105-202024": [("下次赠送寻访", 1), ("以上干员获取概率提升", 1), ("仅限一次", 1), ("查看详情", 1)],
    "20261105-202110": [("日冕重烈", 2), ("已完成专精等级2", 1)],
    "20261105-202137": [("自动回复", 3), ("490%", 1), ("90%的庇护", 1), ("伤害量5%", 1), ("+60%和防御力+60%", 1),
                        ("2.5秒恐惧", 1), ("剑与手炮", 1), ("近卫与狙击", 1)],
    "20261105-202329": [("高级凭证区", 1), ("采购凭证区", 1), ("兑换规则", 1), ("库存更新倒计时", 1), ("剩余15", 5),
                        ("剩余20", 1), ("公开招募★3兑换券", 1), ("公开招募★4兑换券", 1)],
    "20261105-203038": [("仓库", 1), ("任务", 1), ("礼包限时上架", 1), ("塞梦珂城", 1), ("许愿墙", 1), ("感谢庆典", 1),
                        ("限时签到", 1), ("当前", 1), ("唔咕咕……好", 1), ("会……呼……呼", 1), ("基建", 1), ("采购中心", 1)],
    "20261103-215558": [("克莱门莎", 1), ("奥尔佳", 1)],
    "20261103-215611": [("棘刺", 1)],
    "20261104-010912": [("罗比菈塔", 1)],
    "20261104-223233": [("罗比菈塔", 1)],
}


def norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split()).replace("...", "…")


def read(ocr, frame) -> list[str]:
    from ak_translator import layout
    from ak_translator.engine import quarter_lines

    lines = [l for l in ocr.recognize(frame) if l.text.strip()]
    if ocr.needs_reread:
        lines += quarter_lines(ocr, frame, lines)
    return [l.text for l in layout.split_chips(lines, frame)]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="OCR 비교")
    ap.add_argument("--cpu", type=int, default=0, help="PaddleOCR 모델마다 CPU 로도 잴 장 수")
    ap.add_argument("--model", action="append", default=[], metavar="이름=폴더",
                    help="더 견줄 PaddleOCR 모델 (폴더 안에 ppocr/det.onnx·rec.onnx·rec_dict.txt). 여러 번 줄 수 있다")
    ap.add_argument("--no-windows", action="store_true", help="Windows OCR 은 재지 않는다")
    ap.add_argument("--det", metavar="T,B,U", help="검출 문턱값 DET_THRESH,BOX_THRESH,UNCLIP 을 이 값으로 (모든 PaddleOCR 모델)")
    args = ap.parse_args()

    from PIL import Image

    from ak_translator import ppocr, textnorm as tn
    from ak_translator.ocr import WindowsOCR
    from ak_translator.official import OfficialDB

    if args.det:
        ppocr.DET_THRESH, ppocr.BOX_THRESH, ppocr.UNCLIP = (float(v) for v in args.det.split(","))
    db = OfficialDB(os.path.join(os.environ.get("AK_DATA") or config.DATA_DIR, "official.sqlite"))
    caps = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "debug", "*", "1_원본.png")))
    frames = {c: Image.open(os.path.join(ROOT, "debug", c, "1_원본.png")).convert("RGB") for c in caps}
    charset, rare = db.meta.get("charset", ""), db.meta.get("rare", "")
    models = {"paddle": config.MODEL_DIR}
    for spec in args.model:
        name, _sep, folder = spec.partition("=")
        models[name] = folder
    engines = {} if args.no_windows else {"windows": WindowsOCR()}
    for name, folder in models.items():
        engines[name] = ppocr.PaddleOCR(folder, charset, rare=rare)
        engines[name].warm(2560, 1440)
    if args.cpu:
        for name, folder in models.items():
            engines[f"{name}_cpu"] = ppocr.PaddleOCR(folder, charset, rare=rare, cpu=True)
    report = {"caps": caps, "totals": {}, "checks": {}, "lines": {}}
    for name, ocr in engines.items():
        use = caps[:args.cpu + 1] if name.endswith("_cpu") else caps
        times, ok, bad, got_checks, want_checks = [], 0, 0, 0, 0
        report["lines"][name] = {}
        for i, cap in enumerate(use):
            t = time.perf_counter()
            texts = read(ocr, frames[cap])
            if i:
                times.append(time.perf_counter() - t)
            report["lines"][name][cap] = texts
            for text in texts:
                n = tn.cjk_count(text)
                if n >= 2:
                    if db.lookup(text, fuzzy=False) is not None:
                        ok += n
                    else:
                        bad += n
            joined = [norm(t) for t in texts]
            for needle, want in CHECKS.get(cap, []):
                got = sum(t.count(norm(needle)) for t in joined)
                want_checks += want
                got_checks += min(want, got)
                report["checks"].setdefault(cap, {}).setdefault(needle, {"want": want})[name] = got
        speed = sum(times) / max(1, len(times))
        report["totals"][name] = {"verified_cjk": ok, "unverified_cjk": bad, "checks": f"{got_checks}/{want_checks}",
                                  "seconds": round(speed, 3), "backend": getattr(ocr, "backend", "")}
        print(f"{name:10} 확인된 한자 {ok:6,} · 확인 안 된 한자 {bad:5,} · 체크 {got_checks}/{want_checks} · "
              f"{speed:.2f}초/장 {getattr(ocr, 'backend', '')}", flush=True)
    print("\n체크리스트 (원하는 수 / " + " / ".join(engines) + ")")
    for cap, items in report["checks"].items():
        for needle, d in items.items():
            print(f"  {cap} {needle:14} {d['want']} / " + " / ".join(str(d.get(n, "-")) for n in engines))
    out = os.path.join(ROOT, "build", "regress")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "bench_ocr.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
