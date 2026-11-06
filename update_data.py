"""공식 번역 데이터를 새로 받아 data/official.sqlite 를 다시 만든다.

받는 것은 게임 데이터의 텍스트 파일(txt/json)뿐이고, 변환이 끝나면 지운다.
내려받은 폴더의 코드가 import 되는 일이 없도록 변환기는 격리 모드(-I)로 돌린다.
exe 판(UpdateData.exe)은 파이썬이 없으므로 변환기를 같은 프로세스에서 돌린다. 묶인 코드만 import 하므로
내려받은 폴더의 코드는 여기서도 import 되지 않는다. 게임 데이터는 git 으로 받는다 (Git for Windows 필요).
"""
import os
import shutil
import stat
import subprocess
import sys
import traceback
import urllib.request

REPO = "https://github.com/ArknightsAssets/ArknightsGamedata.git"
RAW_URL = "https://raw.githubusercontent.com/ArknightsAssets/ArknightsGamedata/master/"
GIT_URL = "https://git-scm.com/download/win"
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
WORK = os.path.join(DATA, "_raw")
TARGET = os.path.join(DATA, "official.sqlite")
PENDING = TARGET + ".new"


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def remove_tree(path: str) -> None:
    def retry(func, target, _exc):
        # .git 안의 파일은 읽기 전용이라 그냥은 지워지지 않는다
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if os.path.exists(path):
        shutil.rmtree(path, onexc=retry)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if shutil.which("git") is None:
        print("게임 데이터를 받으려면 Git for Windows 가 필요합니다.")
        print(f"  {GIT_URL} 에서 설치한 뒤 다시 실행해 주세요. 기존 데이터는 그대로입니다.")
        return 1
    remove_tree(WORK)
    os.makedirs(WORK)
    repo = os.path.join(WORK, "repo")
    i18n = os.path.join(WORK, "i18n")
    os.makedirs(i18n)

    print("1/3 공식 데이터 내려받는 중 (전체 10분쯤 걸립니다)")
    run("git", "-c", "core.longpaths=true", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", "--no-tags", "--branch", "master", REPO, repo)
    run("git", "-C", repo, "config", "core.longpaths", "true")
    run("git", "-C", repo, "sparse-checkout", "set", "--no-cone",
        "cn/gamedata/story/", "kr/gamedata/story/", "cn/gamedata/excel/", "kr/gamedata/excel/")
    for lang in ("cn", "kr"):
        urllib.request.urlretrieve(
            f"{RAW_URL}{lang}/i18n/string_map.txt", os.path.join(i18n, f"string_map_{lang}.txt")
        )
    revision = run("git", "-C", repo, "rev-parse", "HEAD")

    print("2/3 번역 DB 만드는 중")
    if FROZEN:
        from ak_translator import build_db
        try:
            build_db.build(repo, i18n, PENDING, revision)
            code = 0
        except Exception:   # 변환기 오류를 보여 주고 기존 데이터는 그대로 둔다
            traceback.print_exc()
            code = 1
    else:
        code = subprocess.call([
            sys.executable, "-I", os.path.join(ROOT, "ak_translator", "build_db.py"),
            "--raw", repo, "--i18n", i18n, "--out", PENDING, "--revision", revision,
        ], cwd=ROOT)
    if code != 0:
        print("번역 DB 를 만들지 못했습니다. 기존 데이터는 그대로입니다.")
        return code

    print("3/3 임시 파일 지우는 중")
    if "--keep-raw" not in sys.argv:    # 변환기를 고칠 때 다시 받지 않으려고 남겨 둘 수 있다
        remove_tree(WORK)
    try:
        os.replace(PENDING, TARGET)
        print("완료. 프로그램을 켜면 새 데이터를 씁니다.")
    except PermissionError:
        print("완료. 프로그램이 켜져 있어서, 다음에 켤 때 새 데이터로 바뀝니다.")
    return 0


def frozen_main() -> int:
    """exe 판. 콘솔 창이 바로 닫히지 않게 끝나면(실패해도) 엔터를 기다린다 (스크립트 판은 .bat 의 pause)."""
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        print("받지 못했습니다. 인터넷 연결을 확인하고 다시 실행해 주세요. 기존 데이터는 그대로입니다.")
        code = 1
    try:
        input("\n엔터를 누르면 창이 닫힙니다.")
    except EOFError:    # 콘솔 없이(다른 프로그램에서) 돌렸을 때
        pass
    return code


if __name__ == "__main__":
    sys.exit(frozen_main() if FROZEN else main())
