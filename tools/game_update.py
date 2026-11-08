"""게임 업데이트 반영 (배포자용). 명령 하나로 공식 DB 와 번역 묶음을 새 게임 데이터에 맞춰 GitHub 에 올린다.

  game_update.bat                              # 두 번 눌러 실행 (= python -I tools/game_update.py)
  python -I tools/game_update.py --check       # 무엇을 할지만 본다. 아무것도 바꾸거나 올리지 않는다

하는 일. 단계마다 할 것이 없으면 건너뛰고, 도중에 멈췄으면 다시 돌릴 때 이어서 한다.
  1. 확인     게임 데이터 저장소(ArknightsGamedata)의 최신 커밋을 내 DB 와 견줘 중국·한국 서버 중 어디가 바뀌었는지 본다
  2. DB      update_data.py 로 공식 번역 DB 를 새로 만든다 (Git, 10분쯤). 번역기가 켜져 있으면 official.sqlite.new
             로 만들어지는데, 아래 단계는 그것을 쓴다 (번역기는 다음에 켤 때 바꿔 끼운다)
  3. DB 올리기  릴리스 'db' 에 올라간 판과 다르면 publish_db.py (5분쯤) → gh release upload
  4. 새 글 번역  build_pack (DeepL) → story_pack (DeepL 초벌 → Gemini) → 이번에 새로 번역한 줄만
             polish_pack · polish_pack --slots · fix_names (Gemini 다듬기)
  5. 묶음 올리기  install_pack 검사(+ 내 번역기 data/pack 에 넣기) → 번역 저장소 커밋 → push.
             번역문이 하나도 안 바뀌었으면 올리지 않는다 (쓰는 사람에게 빈 새 판 알림이 가지 않게)

DeepL 한도에 닿으면 번역을 멈추고 알린다 (창 + 끝 값 3). '닿음' 은 남은 한도가 --reserve(기본 5만 자, 게임 중 화면
번역용으로 남기는 몫) 이하가 된 때다. 그때까지 번역한 줄은 번역 저장소 폴더에 저장되지만 올리지는 않는다. 번역기를
정하거나 한도를 늘린 뒤 다시 돌리면 남은 줄부터 이어서 하고 올린다. Gemini 가 하루 한도 등으로 멈춰도 같은 식이다
(끝 값 4, 다음 날 다시 돌리면 이어서). 끝나거나 멈추면 창으로 알린다 (--no-popup 이면 글로만).

번역기 키는 config.json 에서 읽어 환경 변수로만 넘긴다. 화면과 기록 파일에 찍지 않는다.
기록: build/game_update.log · 다듬을 새 줄 목록: build/game_update_new_keys.txt (올리면 비운다)
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_pack  # noqa: E402
import update_data  # noqa: E402
import with_keys  # noqa: E402
from ak_translator import config, dbfetch, version  # noqa: E402
from ak_translator.pack import MANIFEST  # noqa: E402

BUILD = os.path.join(ROOT, "build")
LOG = os.path.join(BUILD, "game_update.log")
NEW_KEYS = os.path.join(BUILD, "game_update_new_keys.txt")
DIST = os.path.join(ROOT, "dist")
GH_DEFAULT = r"C:\Program Files\GitHub CLI\gh.exe"

DONE, FAILED = 0, 1
DEEPL_STOPPED, GEMINI_STOPPED = build_pack.DEEPL_STOPPED, build_pack.GEMINI_STOPPED
TITLE = "명일방주 번역 — 게임 업데이트 반영"

_log = None


def say(text: str = "") -> None:
    print(text, flush=True)
    if _log:
        _log.write(text + "\n")
        _log.flush()


def popup(text: str, kind: str, enabled: bool) -> None:
    """끝났거나 멈췄음을 창으로 알린다. 게임 위에도 보이게 맨 위에 띄우되, 게임에서 초점을 뺏지는 않는다."""
    if not enabled:
        return
    icon = {"info": 0x40, "warn": 0x30, "error": 0x10}[kind]
    try:
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, icon | 0x40000)     # MB_TOPMOST
    except Exception:
        pass


# ------------------------------------------------------------------ 바깥 프로그램
def run_tool(script: str, *args: str, env: dict | None = None) -> int:
    """파이썬 도구를 격리 모드로 돌리고 출력을 그대로 보여 주며 기록에도 남긴다. 끝 값을 돌려준다."""
    cmd = [sys.executable, "-I", script, *args]
    say(f"$ {os.path.relpath(script, ROOT)} {' '.join(args)}")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        say("  " + line.rstrip("\n"))
    return proc.wait()


def git(folder: str, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(out.stderr or out.stdout).strip()}")
    return out.stdout.strip()


def gh_path() -> str | None:
    found = shutil.which("gh")
    return found or (GH_DEFAULT if os.path.exists(GH_DEFAULT) else None)


# ------------------------------------------------------------------ 상태
def db_path() -> str:
    """번역기가 켜져 있어 바꿔 끼우지 못한 새 DB 가 있으면 그것."""
    return update_data.PENDING if os.path.exists(update_data.PENDING) else update_data.TARGET


def db_meta(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT name, value FROM meta"))
    finally:
        con.close()


def gamedata_head() -> str:
    out = subprocess.run(["git", "ls-remote", update_data.REPO, "refs/heads/master"], capture_output=True,
                         text=True, timeout=120, check=True).stdout
    return out.split()[0] if out.strip() else ""


def changed_servers(revision: str) -> set[str]:
    """내 DB 이후 바뀐 서버 ('cn', 'kr'). 커밋 목록을 못 읽으면 둘 다로 친다."""
    import requests

    from ak_translator.app import COMMITS_URL, USED_SERVERS, servers_changed

    try:
        resp = requests.get(COMMITS_URL, headers={"Accept": "application/vnd.github+json"}, timeout=15)
        resp.raise_for_status()
        return servers_changed(resp.json(), revision)
    except Exception:
        return set(USED_SERVERS)


def pack_keys(folder: str) -> set[str]:
    return {k for rows in build_pack.load_existing(folder).values() for k in rows}


def pending_keys() -> set[str]:
    return build_pack.read_keys(NEW_KEYS) if os.path.exists(NEW_KEYS) else set()


def remember_keys(keys: set[str]) -> None:
    os.makedirs(BUILD, exist_ok=True)
    with open(NEW_KEYS, "w", encoding="utf-8") as f:
        f.writelines(k + "\n" for k in sorted(keys))


def remember_new(folder: str, before: set[str]) -> set[str]:
    """before 이후 묶음에 새로 생긴 줄을 다듬을 목록에 더한다. 지금 묶음의 열쇠를 돌려준다."""
    now = pack_keys(folder)
    remember_keys(pending_keys() | (now - before))
    return now


def deepl_left_text() -> str:
    """DeepL 남은 한도 ('12,345자'). 키가 없거나 알 수 없으면 '알 수 없음'."""
    left = None
    if os.environ.get("DEEPL_KEY"):
        try:
            left = build_pack.quota_left(build_pack.make_engine("deepl"))
        except (Exception, SystemExit):
            pass
    return f"{left:,}자" if left is not None else "알 수 없음"


def sync_pack(folder: str, check_only: bool) -> str | None:
    """번역 저장소를 GitHub 와 맞춘다 (웹에서 고친 줄·Action 커밋 받기). 맞출 수 없으면 까닭을 돌려준다."""
    git(folder, "fetch", "-q", "origin")
    behind = int(git(folder, "rev-list", "--count", "HEAD..@{u}") or 0)
    if not behind:
        return None
    if git(folder, "status", "--porcelain"):
        return (f"번역 저장소에 아직 안 올린 변경이 있는데 GitHub 에도 새 커밋이 {behind}개 있습니다.\n"
                f"{folder} 에서 둘을 먼저 합쳐 주세요 (git stash → git pull → git stash pop).")
    if check_only:
        say(f"  번역 저장소: GitHub 에 새 커밋 {behind}개 (실제 실행 때 받는다)")
    else:
        git(folder, "merge", "-q", "--ff-only", "@{u}")
        say(f"  번역 저장소: GitHub 의 새 커밋 {behind}개를 받았습니다")
    return None


# ------------------------------------------------------------------ 본체
def main() -> int:
    global _log
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="게임 업데이트 반영: 공식 DB · 번역 묶음을 새로 만들어 올린다")
    ap.add_argument("--check", action="store_true", help="무엇을 할지만 본다 (아무것도 바꾸지 않는다)")
    ap.add_argument("--pack", default=os.path.join(os.path.dirname(ROOT), "ak-translations"), help="번역 저장소 폴더")
    ap.add_argument("--reserve", type=int, default=50000,
                    help="DeepL 남은 한도가 이 이하가 되면 멈춘다 (게임 중 화면 번역용 몫, 기본 5만 자)")
    ap.add_argument("--skip-db", action="store_true", help="DB 만들기·올리기를 건너뛰고 번역만 한다")
    ap.add_argument("--force-db", action="store_true", help="게임 데이터가 같아도 DB 를 다시 만든다")
    ap.add_argument("--no-push", action="store_true", help="번역 저장소에 커밋만 하고 올리지 않는다")
    ap.add_argument("--no-install", action="store_true", help="검사만 하고 내 번역기 data/pack 에는 넣지 않는다 (시험용)")
    ap.add_argument("--no-popup", action="store_true", help="끝나거나 멈췄을 때 창을 띄우지 않는다")
    args = ap.parse_args()

    os.makedirs(BUILD, exist_ok=True)
    _log = open(LOG, "a", encoding="utf-8")
    say(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M} 게임 업데이트 반영{' (확인만)' if args.check else ''}")
    try:
        code, message = run(args)
    except Exception as exc:     # 예상 못 한 오류도 알리고 끝낸다
        code, message = FAILED, f"오류로 멈췄습니다: {type(exc).__name__}: {exc}"
    say("")
    say(message)
    say(f"(기록: {LOG})")
    if code == DONE:
        kind = "info"
    elif code in (DEEPL_STOPPED, GEMINI_STOPPED):
        kind = "warn"
    else:
        kind = "error"
    popup(message, kind, not args.no_popup and not args.check)
    _log.close()
    return code


def run(args) -> tuple[int, str]:
    cfg = config.load()
    with_keys.load_keys(cfg)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    pack = args.pack
    if shutil.which("git") is None:
        return FAILED, "Git 이 없습니다. Git for Windows 를 설치해 주세요."
    gh = gh_path()
    if gh is None and not args.check:
        return FAILED, "GitHub 명령줄 도구(gh)가 없습니다. winget install GitHub.cli 로 설치해 주세요."
    if not os.path.isdir(os.path.join(pack, ".git")):
        return FAILED, f"번역 저장소 폴더가 없습니다: {pack}"
    problem = sync_pack(pack, args.check)
    if problem:
        return FAILED, problem

    # 1. 확인
    say("\n[1/5] 게임 데이터 확인")
    meta = db_meta(db_path())
    head = gamedata_head()
    mine = meta.get("revision", "")
    # 영어·일본·대만 서버만 바뀐 커밋은 DB 가 같다. 다시 만들어 올리면 쓰는 사람에게 빈 새 판 알림만 간다
    servers = changed_servers(mine) if head != mine else set()
    new_data = bool(servers)
    if new_data:
        from ak_translator.app import SERVER_NAMES     # changed_servers 가 이미 읽어 둔 모듈

        names = "·".join(SERVER_NAMES[s] for s in sorted(servers))
        say(f"  {names} 서버에 새 게임 데이터 (내 DB {mine[:10] or '없음'} → 최신 {head[:10]})")
    elif head != mine:
        say(f"  중국·한국 서버는 그대로 (다른 서버만 바뀜, 최신 {head[:10]})")
    else:
        say(f"  게임 데이터 그대로 (커밋 {head[:10]}, DB 만든 날 {meta.get('built_at', '?')})")

    # 2. DB 만들기
    say("\n[2/5] 공식 번역 DB 만들기")
    if args.skip_db:
        say("  건너뜀 (--skip-db)")
    elif not (new_data or args.force_db):
        say("  할 것 없음")
    elif args.check:
        say("  새로 만든다 (Git 으로 받아 10분쯤)")
    else:
        code = run_tool(os.path.join(ROOT, "update_data.py"), env=env)
        if code != 0:
            return FAILED, "공식 번역 DB 를 만들지 못했습니다. 기존 DB 는 그대로입니다. 기록을 확인해 주세요."
        meta = db_meta(db_path())
        say(f"  DB: {os.path.relpath(db_path(), ROOT)} (커밋 {meta.get('revision', '')[:10]}, {meta.get('built_at', '?')})")

    # 3. DB 올리기
    say("\n[3/5] 공식 번역 DB 올리기 (릴리스 db)")
    remote = dbfetch.remote_info(dbfetch.base_url(cfg))
    published = remote is not None and not dbfetch.is_newer(meta, remote)
    if args.skip_db:
        say("  건너뜀 (--skip-db)")
    elif published:
        say(f"  이미 올라가 있음 ({remote.get('built_at')})")
    elif args.check:
        say(f"  올린다 (올라간 판 {remote.get('built_at') if remote else '없음'} → 내 DB {meta.get('built_at', '?')})")
    else:
        code = run_tool(os.path.join(ROOT, "tools", "publish_db.py"), "--db", db_path(), env=env)
        if code != 0:
            return FAILED, "공식 DB 를 묶지 못했습니다. 기록을 확인해 주세요."
        up = subprocess.run([gh, "release", "upload", "db", os.path.join(DIST, dbfetch.ZIP_NAME),
                             os.path.join(DIST, dbfetch.INFO_NAME), "--clobber", "--repo", version.REPO],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if up.returncode != 0:
            return FAILED, f"공식 DB 를 GitHub 에 올리지 못했습니다: {(up.stderr or up.stdout).strip()[:300]}"
        say("  올렸습니다. 쓰는 사람은 데이터 탭에서 새 공식 데이터 알림을 받습니다.")

    # 4. 새 글 번역
    say("\n[4/5] 새 글 번역")
    say(f"  DeepL 남은 한도: {deepl_left_text()} (멈추는 선 {args.reserve:,}자)")
    db_arg = ["--db", db_path(), "--out", pack]
    if args.check:
        run_tool(os.path.join(ROOT, "tools", "build_pack.py"), *db_arg, "--dry-run", "--with-story", env=env)
        waiting = pending_keys()
        if waiting:
            say(f"  지난번에 번역하고 아직 안 다듬은 줄 {len(waiting):,}개 → 다듬는다")
        return DONE, "확인만 했습니다. 바꾼 것은 없습니다."

    before = pack_keys(pack)
    stopped = None

    code = run_tool(os.path.join(ROOT, "tools", "build_pack.py"), *db_arg, "--reserve", str(args.reserve), env=env)
    now = remember_new(pack, before)
    if code == DEEPL_STOPPED:
        stopped = DEEPL_STOPPED
    elif code != 0:
        return FAILED, f"번역 묶음 만들기(build_pack)가 실패했습니다 (끝 값 {code}). 기록을 확인해 주세요."

    if stopped is None:
        code = run_tool(os.path.join(ROOT, "tools", "story_pack.py"), *db_arg, "--reserve", str(args.reserve),
                        "--stop-at-deepl-limit", env=env)
        now = remember_new(pack, before)
        if code in (DEEPL_STOPPED, GEMINI_STOPPED):
            stopped = code
        elif code != 0:
            return FAILED, f"스토리 번역(story_pack)이 실패했습니다 (끝 값 {code}). 기록을 확인해 주세요."

    todo = pending_keys() & now     # 묶음은 마지막으로 읽은 뒤 바뀌지 않았다
    if stopped is None and todo:
        say(f"  새로 번역한 줄 {len(todo):,}개를 다듬습니다")
        for script, extra in (("polish_pack.py", ()), ("polish_pack.py", ("--slots",)), ("fix_names.py", ())):
            code = run_tool(os.path.join(ROOT, "tools", script), *db_arg, *extra, "--keys", NEW_KEYS, env=env)
            if code == GEMINI_STOPPED:
                stopped = code
                break
            if code != 0:
                return FAILED, f"다듬기({script} {' '.join(extra)})가 실패했습니다 (끝 값 {code}). 기록을 확인해 주세요."

    if stopped is not None:
        added = len(pending_keys())
        if stopped == DEEPL_STOPPED:
            return DEEPL_STOPPED, (
                f"DeepL 한도에 닿아 번역을 멈췄습니다 (남은 한도 {deepl_left_text()}, 멈추는 선 {args.reserve:,}자).\n"
                f"이번에 번역한 줄 {added:,}개는 번역 저장소 폴더에 저장했지만 아직 올리지 않았습니다.\n"
                "남은 줄을 어떻게 번역할지(다른 번역기·새 키) 정한 뒤 다시 돌리면 이어서 하고 올립니다.")
        return GEMINI_STOPPED, (
            "Gemini 가 멈췄습니다 (하루 한도 등).\n"
            f"이번에 번역한 줄 {added:,}개는 번역 저장소 폴더에 저장했지만 아직 올리지 않았습니다.\n"
            "내일 다시 돌리면 이어서 하고 올립니다.")

    # 5. 묶음 올리기
    say("\n[5/5] 번역 묶음 검사·올리기")
    # 'XY 경로' 꼴. git() 이 앞뒤 빈칸을 깎아 첫 줄의 ' M' 이 'M' 이 되므로 자리로 자르지 않고 빈칸으로 나눈다
    changed = [line.split(maxsplit=1)[-1].strip('"') for line in git(pack, "status", "--porcelain").splitlines()]
    if all(p == MANIFEST for p in changed):
        if MANIFEST in changed:
            git(pack, "checkout", "--", MANIFEST)    # 판 번호만 바뀐 것. 빈 새 판을 내지 않는다
        remember_keys(set())
        say("  바뀐 번역이 없어 올리지 않았습니다.")
        return DONE, "게임 업데이트를 반영했습니다. 번역 묶음은 바뀐 것이 없어 그대로입니다."
    code = run_tool(os.path.join(ROOT, "tools", "install_pack.py"), "--out", pack,
                    "--check" if args.no_install else "--stage", env=env)
    if code != 0:
        return FAILED, "번역 묶음 검사에 걸린 줄이 있어 올리지 않았습니다. 기록의 '문제' 줄을 확인해 주세요."
    with open(os.path.join(pack, MANIFEST), encoding="utf-8") as f:
        pack_version = json.load(f)["version"]
    git(pack, "add", "-A")
    git(pack, "commit", "-q", "-m", f"번역 묶음 {pack_version}")
    if args.no_push:
        return DONE, f"번역 묶음 {pack_version} 을 커밋했습니다 (--no-push 라 올리지 않음)."
    git(pack, "push", "-q")
    remember_keys(set())
    say(f"  올렸습니다: 번역 묶음 {pack_version}")
    return DONE, (f"게임 업데이트를 반영했습니다. 번역 묶음 {pack_version} 을 올렸습니다.\n"
                  "쓰는 사람은 켤 때 새 번역 묶음 알림을 받습니다. 내 번역기는 다시 켜면 새 묶음을 씁니다.")


if __name__ == "__main__":
    sys.exit(main())
