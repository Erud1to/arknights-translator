"""화면 글자를 resolver 에 넣어 어느 길(공식·묶음·캐시·…)로 무엇이 나오는지 본다. 기계 번역은 부르지 않는다.

  python -I tools/probe.py "剩余15" "一冻河" "下次赠这寻访．8小时"
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import config  # noqa: E402
from ak_translator.resolver import Resolver  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    res = Resolver(config.load(), os.environ.get("AK_DATA") or config.DATA_DIR)
    res.enqueue = lambda *a, **k: None
    for _ in range(200):    # 이름 사전은 뒤에서 만든다
        if res.names.entries:
            break
        time.sleep(0.1)
    for text in sys.argv[1:]:
        got = res.resolve(text, allow_mt=False)
        print(f"{text!r:40} → {got.via:9} {(got.text or '')!r:.70} trim={got.trim} missed={got.missed}")
    res.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
