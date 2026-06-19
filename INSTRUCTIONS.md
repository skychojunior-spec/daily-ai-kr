# 데일리 실행 지침 (Claude가 매일 따르는 규칙)

이 파일은 매일 자동 실행될 때 Claude가 그날의 `content.json`을 만들기 위해 따르는 편집 규칙입니다.
**기계적인 발행(음성·피드·깃·디스코드)은 `publish.py`가 하고, 내용 만들기는 이 지침대로 Claude가 합니다.**

## 1. 수집 (미국 · LLM · 빅테크 중심)
다음을 그날 기준으로 조사한다 (WebSearch / WebFetch / arXiv 도구 사용):
- **뉴스 소스**: OpenAI, Anthropic, Google DeepMind, Meta AI 공식 블로그 / TechCrunch AI / The Verge AI / The Information
- **논문 소스**: arXiv `cs.CL`·`cs.AI` 신규, Hugging Face Daily Papers
- **반드시 미국 중심 + LLM/빅테크 주제만.** 한국·기타 지역, LLM과 무관한 일반 IT는 제외.

## 2. 선별
- 그날 **가장 중요한 3~5건**만 고른다 (뉴스 위주 + 논문 1~2건 포함 권장).
- 중복·홍보성·루머는 버린다. 출처 URL을 반드시 확보한다.

## 3. 한국어 요약 (비개발자 눈높이)
각 건마다:
- **헤드라인**: 한 줄 한국어
- **핵심 3줄**: 무슨 일인지 / 왜 중요한지 / 나에게 무슨 의미인지
- 전문용어는 괄호로 쉬운 설명 (예: "파인튜닝(기존 AI를 특정 용도로 더 훈련시키는 것)")

## 4. 2인 대화 대본 (5분 분량, 약 700~900자)
- **host(진행자, 여성 목소리)**: 질문하고 정리하는 역할. 청취자 대변.
- **expert(전문가, 남성 목소리)**: 설명하는 역할. 쉽게 풀어줌.
- 인사 → 오늘의 주제들 → 각 건을 대화로 풀기 → 한 줄 마무리.
- 너무 딱딱하지 않게, 라디오처럼 자연스럽게. 한 turn은 1~3문장.

## 5. 출력: content.json 작성
아래 형식으로 `content.json`을 만든다:

```json
{
  "date": "YYYY-MM-DD",
  "episode_title": "6월 19일 — OpenAI 신모델, 구글 논문 외 3건",
  "show_notes": "팟캐스트 설명란에 들어갈 2~3줄 요약",
  "items": [
    {"headline": "한 줄 헤드라인", "url": "https://출처"}
  ],
  "dialogue": [
    {"speaker": "host", "text": "안녕하세요, AI 데일리입니다…"},
    {"speaker": "expert", "text": "네, 오늘 첫 소식은…"}
  ],
  "discord_text": "**📰 오늘의 AI 브리핑 (6/19)**\n\n**1. 헤드라인**\n- 핵심 3줄…\n🔗 출처\n\n**2. …**"
}
```

- `discord_text`는 Discord 마크다운. 각 건마다 헤드라인+핵심+출처 링크. 1900자 이내.
- 작성 후 반드시 `python3 publish.py content.json` 실행.
