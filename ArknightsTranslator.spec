# PyInstaller 설정. 직접 돌리지 말고 tools/make_release.py 를 쓴다 (빌드 → 묶음 복사 → zip).
#
#   dist/ArknightsTranslator/
#     ArknightsTranslator.exe   번역 오버레이 (창)
#     UpdateData.exe            공식 데이터 갱신 (콘솔, update_data.py)
#     _internal/                두 exe 가 같이 쓰는 파이썬·라이브러리
#
# 한 파일 exe(onefile)는 켤 때마다 임시 폴더에 풀어 느리고 백신 오탐도 잦아 폴더로 묶는다. UPX 도 같은 이유로 끈다.

EXCLUDES = ["tools", "pytest", "IPython", "matplotlib", "pandas", "scipy", "torch", "pydoc", "unittest"]

# Pretendard(SIL OFL)를 라이선스와 함께 _internal/fonts 에 넣는다. 설치하지 않고 켤 때 이 프로세스에만 등록한다.
# PaddleOCR 모델(Apache-2.0, NOTICE.txt)은 _internal/models. onnxruntime 은 DirectML 판이어야 한다 (make_release 가 확인)
app = Analysis(["main.py"], excludes=EXCLUDES, datas=[("fonts", "fonts"), ("models", "models")])
# opencv 는 원문 지우기(inpaint)에만 쓴다. 동영상용 ffmpeg DLL(개당 30MB)은 뺀다
app.binaries = [b for b in app.binaries if "opencv_videoio_ffmpeg" not in b[0]]
updater = Analysis(["update_data.py"], excludes=EXCLUDES + ["numpy", "PIL", "cv2", "mss", "winsdk", "tkinter"])

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="ArknightsTranslator",
              console=False, upx=False)
updater_exe = EXE(PYZ(updater.pure), updater.scripts, [], exclude_binaries=True, name="UpdateData",
                  console=True, upx=False)
COLLECT(app_exe, app.binaries, app.datas, updater_exe, updater.binaries, updater.datas,
        name="ArknightsTranslator", upx=False)
