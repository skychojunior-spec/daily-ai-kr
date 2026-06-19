# AI 데일리 — 미국 LLM·빅테크 한국어 브리핑 🎧

매일 아침, 미국의 **LLM·빅테크** 최신 **뉴스와 논문**을 한국어로 요약해
**Discord 텍스트** + **2인 대화형 음성(개인 팟캐스트)** 으로 자동 전송합니다.
**완전 무료** 스택 (edge-tts · ffmpeg · GitHub Pages · Discord 웹훅).

## 구성
| 파일 | 역할 |
|------|------|
| `INSTRUCTIONS.md` | 매일 Claude가 따르는 편집 지침 (수집·요약·대본 규칙) |
| `publish.py` | 발행 엔진 — 음성 생성→합치기→RSS 갱신→GitHub push→Discord 전송 |
| `config.json` | 팟캐스트 정보·음성·GitHub 설정 |
| `secrets.env` | Discord 웹훅 (깃에 안 올라감) |
| `docs/` | GitHub Pages 호스팅 폴더 (feed.xml + ep/*.mp3) |

## 매일 흐름
1. Claude가 `INSTRUCTIONS.md`대로 리서치 → `content.json` 생성
2. `python3 publish.py content.json` 실행 → 음성·피드·발행·Discord 전송

## 팟캐스트 구독
RSS 주소를 팟캐스트 앱(Apple Podcasts / Pocket Casts / Overcast 등)에 추가:
```
https://skychojunior-spec.github.io/daily-ai-kr/feed.xml
```

## 직접 한 번 실행해보기
```bash
python3 publish.py content.json
```
