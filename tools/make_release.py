"""배포용 zip 만들기 (배포자용).

  python -I tools/make_release.py                      # dist/ArknightsTranslator-v<판>-<날짜>.zip
  python -I tools/make_release.py --pack ..\\ak-translations

빌드하는 파이썬의 onnxruntime 은 DirectML 판(onnxruntime-directml)이어야 한다. 그래야 받은 사람 PC 의 그래픽 카드
(NVIDIA·AMD·인텔)로 PaddleOCR 이 돈다. CUDA 판(onnxruntime-gpu)은 CUDA 런타임이 따로 있어야 해서 싣지 않는다.
개발 PC 에 onnxruntime-gpu 가 깔려 있으면 따로 가상환경을 만들어 그 파이썬으로 돌린다:
  python -m venv --system-site-packages .venv-release
  .venv-release\\Scripts\\python -m pip install onnxruntime-directml
  .venv-release\\Scripts\\python -I tools\\make_release.py

  1. PyInstaller 로 exe 두 개를 묶는다 (ArknightsTranslator.spec)
  2. 번역 묶음(manifest.json + TSV)을 data/pack 에 넣는다. 처음 켜자마자 묶음 번역이 나오게
  3. 사용법.txt 를 쓰고 zip 으로 묶는다

개인 파일은 넣지 않는다: config.json(번역기 키), data/official.sqlite(사용자가 처음 켤 때 받는다), 번역캐시·용어집,
debug/ (문제 화면). 마지막에 내 config.json 의 키가 결과물 어디에도 없는지 한 번 더 훑는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "ArknightsTranslator"
SECRET_KEYS = ("deepl_key", "gemini_key", "papago_id", "papago_secret")
GUIDE = """명일방주 번역 오버레이

중국 서버 명일방주(PC)의 화면 글자를 읽어 그 자리에 한국어를 덮어 보여 줍니다.
한국 서버의 공식 번역을 먼저 쓰고, 공식 번역이 없는 글은 미리 번역해 둔 '번역 묶음'을 씁니다.

처음 한 번
  ArknightsTranslator.exe 를 켜면 공식 번역 데이터(약 120MB)를 받을지 묻습니다. '예' 를 누르면 1~2분 뒤에
  바로 켜집니다. 따로 설치할 것은 없습니다 (번역기 키, CUDA, Git 모두 필요 없음).

쓰기
  게임을 '테두리 없는 전체 화면' 이나 '창 모드' 로 두고 이 프로그램을 켜 두면 알아서 붙습니다.
    Ctrl+Alt+H  번역 켜기/끄기          Ctrl+Alt+T  지금 번역 (전투 중에도)
    Ctrl+Alt+O  누르는 동안 원문 보기   Ctrl+Alt+S  문제 화면 저장          Ctrl+Alt+Q  종료
  지나간 스토리 대사는 패널의 '스토리' 탭에서 다시 읽을 수 있습니다.
  번역기 키는 넣지 않아도 됩니다. 공식 번역·번역 묶음에 없는 글만 구글 번역으로 옮깁니다
  (설정 탭에서 DeepL·Papago 키를 넣을 수도 있음).

번역이 이상하면
  - 내 PC 에서 바로 고치기: '최근 번역'·'스토리' 탭에서 그 줄을 고르고 '고치기'.
  - 모두에게 고쳐지게 제보: 같은 자리의 '오역 제보' 를 누르면 제보 화면이 브라우저로 열립니다
    (원문·번역이 채워져 있습니다. 고칠 번역을 적고 제출하세요. GitHub 계정이 필요합니다).

갱신
  - 번역 묶음: 데이터 탭의 '번역 묶음 받기'. 새 판이 나오면 켤 때 알려 줍니다.
  - 공식 데이터: 새 데이터가 올라오면 데이터 탭에 알려 줍니다. '공식 데이터 갱신' 을 누르면 받습니다.
  - 프로그램: 새 판이 나오면 데이터 탭에 '새 판 받기' 가 생깁니다.

파일
  config.json   설정 (번역기 키 포함 — 남에게 보내지 마세요)
  data\\         번역 DB, 번역 묶음, 번역캐시(화면에서 번역한 글), 용어집(내가 고친 번역)

글자 인식
  그래픽 카드가 있으면 PaddleOCR 로 읽고(더 정확), 없거나 느리면 Windows 내장 OCR 로 읽습니다.
  설정 탭의 '글자 인식' 에서 바꿀 수 있습니다. 처음 켤 때 PaddleOCR 준비에 몇 초 걸립니다.

글꼴·모델
  Pretendard (Copyright 2021 Kil Hyung-jin, SIL Open Font License 1.1) 를 같이 넣었습니다.
  라이선스 전문: _internal\\fonts\\OFL.txt. PC 에 설치하지 않고 이 프로그램 안에서만 씁니다.
  PaddleOCR 글자 검출·인식 모델 (PaddlePaddle, Apache License 2.0): _internal\\models\\ppocr\\NOTICE.txt
"""


def check_runtime() -> None:
    """이 파이썬의 onnxruntime 이 DirectML 판인가. 아니면 배포판의 PaddleOCR 이 GPU 를 못 쓴다."""
    try:
        import onnxruntime
    except ImportError:
        raise SystemExit("onnxruntime 이 없습니다. onnxruntime-directml 을 설치한 파이썬으로 만드세요 (맨 위 설명).")
    if "DmlExecutionProvider" not in onnxruntime.get_available_providers():
        raise SystemExit(f"이 파이썬의 onnxruntime({onnxruntime.__version__}, {onnxruntime.__file__})은 DirectML 판이 "
                         "아닙니다. onnxruntime-directml 을 설치한 가상환경으로 만드세요 (make_release.py 맨 위 설명).")


def build(work: str) -> str:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--workpath", work, "--distpath", os.path.join(ROOT, "dist"),
                    os.path.join(ROOT, f"{NAME}.spec")], cwd=ROOT, check=True)
    return os.path.join(ROOT, "dist", NAME)


def copy_pack(src: str, out: str) -> str:
    with open(os.path.join(src, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    dest = os.path.join(out, "data", "pack")
    os.makedirs(dest, exist_ok=True)
    for name in ["manifest.json", *manifest["files"]]:
        shutil.copy2(os.path.join(src, name), dest)
    return manifest["version"]


def secrets() -> list[str]:
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return []
    return [str(cfg[k]) for k in SECRET_KEYS if len(str(cfg.get(k) or "")) >= 8]


def check_clean(out: str) -> None:
    """개인 파일이 들어가지 않았는지, 내 번역기 키가 어느 파일에도 없는지."""
    for banned in ("config.json", os.path.join("data", "official.sqlite"), os.path.join("data", "번역캐시.md"),
                   os.path.join("data", "용어집.md"), "debug"):
        if os.path.exists(os.path.join(out, banned)):
            raise SystemExit(f"배포판에 개인 파일이 들어갔습니다: {banned}")
    needles = [s.encode("utf-8") for s in secrets()]
    for folder, _dirs, files in os.walk(out):
        for name in files:
            with open(os.path.join(folder, name), "rb") as f:
                data = f.read()
            if any(n in data for n in needles):
                raise SystemExit(f"배포판 파일에 번역기 키가 들어 있습니다: {os.path.join(folder, name)}")


def make_zip(out: str, path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _dirs, files in os.walk(out):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.join(NAME, os.path.relpath(full, out)))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="배포용 zip 만들기")
    default_pack = os.path.join(os.path.dirname(ROOT), "ak-translations")
    ap.add_argument("--pack", default=default_pack if os.path.isdir(default_pack) else os.path.join(ROOT, "data", "pack"),
                    help="넣을 번역 묶음 폴더 (기본: 번역 저장소 ..\\ak-translations)")
    ap.add_argument("--work", default=os.path.join(ROOT, "build"), help="PyInstaller 작업 폴더")
    ap.add_argument("--no-build", action="store_true", help="dist 에 있는 빌드를 그대로 쓴다")
    ap.add_argument("--test", action="store_true", help="시험 빌드: 저장소(version.REPO)가 비어 있어도 만든다")
    args = ap.parse_args()

    sys.path.insert(0, ROOT)
    from ak_translator import version

    if not version.REPO and not args.test:
        # 저장소가 없으면 사용자는 공식 DB 를 받지 못하고 Git 을 깔아 10분 동안 직접 만들어야 한다
        raise SystemExit("ak_translator/version.py 의 REPO('owner/name')를 먼저 정해 주세요. 사용자는 거기 릴리스 'db' 에서\n"
                         "공식 DB 를 받고 새 판 알림도 거기서 봅니다 (시험 빌드는 --test).")
    if not args.no_build:
        check_runtime()
    out = os.path.join(ROOT, "dist", NAME) if args.no_build else build(args.work)
    models = os.path.join(out, "_internal", "models", "ppocr")
    if not all(os.path.exists(os.path.join(models, f)) for f in ("det.onnx", "rec.onnx", "rec_dict.txt")):
        raise SystemExit(f"PaddleOCR 모델이 빌드에 없습니다: {models}")
    version = copy_pack(args.pack, out)
    with open(os.path.join(out, "사용법.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(GUIDE)
    check_clean(out)
    from ak_translator.version import VERSION

    path = os.path.join(ROOT, "dist", f"{NAME}-v{VERSION}-{dt.date.today():%Y%m%d}.zip")
    make_zip(out, path)
    print(f"{path} ({os.path.getsize(path) / 1e6:.0f}MB, 번역 묶음 {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
