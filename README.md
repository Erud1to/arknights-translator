# 명일방주 번역 오버레이

중국 서버 명일방주(PC 클라이언트) 화면의 중국어를 읽어, **그 자리에 한국어를 덮어 보여 주는** 프로그램입니다.

- 한국 서버에 나온 내용은 **한국 서버 공식 번역** 그대로 나옵니다 (오퍼레이터·스킬·아이템·스토리 대사).
- 한국 서버에 아직 없는 내용은 미리 번역해 둔 [번역 묶음](https://github.com/Erud1to/ak-translations)을 씁니다.

## 받기

1. [Releases](https://github.com/Erud1to/arknights-translator/releases/latest) 에서 `ArknightsTranslator-v….zip` 을 받아
   원하는 폴더에 풉니다.
2. `ArknightsTranslator.exe` 를 켭니다.
3. 처음 한 번, 공식 번역 데이터(약 120MB)를 받을지 물으면 **예** 를 누릅니다. 1~2분 뒤에 켜집니다.

게임은 **테두리 없는 전체 화면** 이나 **창 모드** 로 두세요. 게임이 켜져 있으면 알아서 붙고, 나중에 켜도 기다렸다가
붙습니다. 오버레이는 마우스 클릭을 통과시켜 조작을 막지 않습니다. 제어 패널은 보조 모니터가 있으면 그쪽에 뜹니다.


## 단축키

| 단축키 | 기능 |
|---|---|
| `Ctrl + Alt + H` | 번역 켜기 / 끄기 |
| `Ctrl + Alt + O` | 누르고 있는 동안 원문 보기 |
| `Ctrl + Alt + T` | 지금 번역 (전투 중에도) |
| `Ctrl + Alt + S` | 문제 화면 저장 |
| `Ctrl + Alt + Q` | 종료 |

## 패널

| 탭 | 내용 |
|---|---|
| 최근 번역 | 띄운 번역과 출처. 줄을 고르면 원문이 보입니다 |
| 스토리 | 지나간 스토리 대사를 다시 읽습니다 |
| 설정 | 표시 방식, 글자 크기·글꼴, 전투 중 쉬기, 공개모집 태그 조합 표시 |
| 데이터 | 공식 데이터·번역 묶음 갱신, 새 판 알림, 오류 기록 |

## 번역이 이상하면

- **내 PC 에서 바로 고치기**: `최근 번역` · `스토리` 탭에서 그 줄을 고르고 `고치기`.
- **모두에게 고쳐지게 제보**: 같은 자리의 `오역 제보 ↗`. 원문과 번역이 채워진 제보 화면이 브라우저로 열립니다
  (GitHub 계정 필요).
- **글자를 잘못 읽거나 번역이 안 뜨면**: 그 화면에서 `Ctrl + Alt + S` 로 저장하고(데이터 탭 → 문제 화면 폴더),
  [Issues](https://github.com/Erud1to/arknights-translator/issues) 에 붙여 주세요.

## 갱신

- **프로그램**: 새 판이 나오면 데이터 탭에 `새 판 받기` 가 뜹니다. 받은 zip 을 원래 폴더에 덮어 풀면 설정과 데이터는
  그대로 남습니다.
- **공식 데이터 · 번역 묶음**: 새 판이 있으면 켤 때 알려 줍니다. 데이터 탭에서 받습니다.

## 출처

- 공식 번역 데이터: [ArknightsAssets/ArknightsGamedata](https://github.com/ArknightsAssets/ArknightsGamedata) 의 중국·한국
  서버 게임 데이터로 만듭니다.
- 글자 인식: [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 모델 (Apache License 2.0)
- 글꼴: [Pretendard](https://github.com/orioncactus/pretendard) (SIL Open Font License 1.1)

명일방주(Arknights)와 게임 글의 권리는 Hypergryph 와 각 서버 운영사에 있습니다. 이 프로그램은 비공식 팬 도구입니다.
