"""config.json 의 번역기 키를 환경 변수로 넘겨 묶음 도구를 돌린다. 키는 화면에 찍지 않는다.

  python -I tools/with_keys.py story_pack [인자...]    # 스토리 (DeepL 초벌 → Gemini 인물 말투)
  python -I tools/with_keys.py build_pack [인자...]    # 번역 묶음 (DeepL)
  python -I tools/with_keys.py polish_pack [인자...]   # 짧은 글 말투 다듬기 (Gemini)

환경 변수(DEEPL_KEY, GEMINI_KEY, PAPAGO_ID, PAPAGO_SECRET)가 이미 있으면 그것을 쓴다.
--out 을 주지 않으면 이 폴더 옆의 ak-translations (번역 저장소) 에 쓴다.
"""
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
TOOLS = ("build_pack", "story_pack", "polish_pack", "fix_names")
KEYS = (("DEEPL_KEY", "deepl_key"), ("GEMINI_KEY", "gemini_key"),
        ("PAPAGO_ID", "papago_id"), ("PAPAGO_SECRET", "papago_secret"))


def load_keys(cfg: dict) -> None:
    """config 의 번역기 키를 환경 변수로 넘긴다. 환경 변수가 이미 있으면 그것을 둔다."""
    for env, key in KEYS:
        if cfg.get(key) and not os.environ.get(env):
            os.environ[env] = str(cfg[key])


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TOOLS:
        raise SystemExit(__doc__)
    from ak_translator import config

    load_keys(config.load())
    name, args = sys.argv[1], sys.argv[2:]
    if "--out" not in args:
        args = ["--out", os.path.join(os.path.dirname(ROOT), "ak-translations"), *args]
    module = importlib.import_module(name)
    sys.argv = [f"{name}.py", *args]
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
