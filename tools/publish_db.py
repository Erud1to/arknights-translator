"""공식 번역 DB 를 사용자가 받을 수 있게 묶는다 (배포자용). 사용자는 Git·10분 빌드 없이 이 파일을 받는다.

  python -I tools/publish_db.py                     # data/official.sqlite → dist/official-db.zip + official-db.json
  python -I tools/publish_db.py --db data/official.sqlite.new

올리기 (처음 한 번만 릴리스를 만든다. 저장소는 version.REPO 와 같은 곳):
  gh release create db --title "공식 번역 데이터" --notes "번역기가 받는 공식 번역 DB" --latest=false
  gh release upload db dist/official-db.zip dist/official-db.json --clobber

게임이 업데이트되면: update_data.bat 로 DB 를 다시 만들고(번역기를 다시 켜 바꿔 끼운 뒤) 이 도구 → upload 를 다시 한다.
번역기는 켤 때 official-db.json 의 판(게임 데이터 커밋·만든 날)이 자기 DB 와 다르면 데이터 탭에 알린다.
"""
import argparse
import os
import sqlite3
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ak_translator import dbfetch  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="공식 번역 DB 배포 파일 만들기")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "official.sqlite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("DB 검사(quick_check)에 실패했습니다. 다시 만들어 주세요.")
        meta = dict(con.execute("SELECT name, value FROM meta").fetchall())
    finally:
        con.close()
    for need in ("built_at", "revision", "charset"):
        if not meta.get(need):
            raise SystemExit(f"DB meta 에 {need} 가 없습니다 — update_data 로 만든 DB 인지 확인해 주세요.")
    os.makedirs(args.out, exist_ok=True)
    zip_path = os.path.join(args.out, dbfetch.ZIP_NAME)
    t = time.time()
    tmp = zip_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_LZMA) as z:
        z.write(args.db, dbfetch.DB_MEMBER)
    os.replace(tmp, zip_path)
    info = dbfetch.info_for(args.db, zip_path)
    dbfetch.write_info(info, os.path.join(args.out, dbfetch.INFO_NAME))
    print(f"{zip_path}  {info['zip']['size'] / 1e6:.0f}MB (DB {info['db']['size'] / 1e6:.0f}MB, {time.time() - t:.0f}초)")
    print(f"판: 만든 날 {info['built_at']} · 게임 데이터 {info['revision'][:12]} · 공식 번역 {info['texts_with_ko']:,}문장")
    print("올리기: gh release upload db dist/official-db.zip dist/official-db.json --clobber")
    return 0


if __name__ == "__main__":
    sys.exit(main())
