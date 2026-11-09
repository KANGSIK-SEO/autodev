# 24/7 Coding Agent

GitHub Issues -> GPT-4o-mini -> PR 자동 제출.

## 흐름

```
이슈 열림
   ↓
GitHub Actions 트리거 (또는 30분 cron)
   ↓
coding_agent.py 실행
   ↓
GPT-4o-mini 코드 생성
   ↓
브랜치 생성 + 파일 커밋
   ↓
PR 자동 오픈 + 이슈 코멘트
```

## 설정 (1회)

1. GitHub 리포 만들기 (이 폴더 내용 push)
2. **Settings → Secrets → Actions** 에서 추가:
   - `OPENAI_API_KEY` : OpenAI 키

`GITHUB_TOKEN` 은 Actions가 자동 제공.

## 실행 조건

| 트리거 | 조건 |
|--------|------|
| 이슈 오픈 | 즉시 |
| 스케줄 | 매 30분 |
| 수동 | Actions 탭 → Run workflow |

## 파일 구조

```
autodev/
├── .github/workflows/coding_agent.yml   # 트리거 & 실행
├── agent/coding_agent.py                # 핵심 에이전트
├── solutions/                           # 생성된 코드 (자동 생성)
└── logs/                                # 실행 로그
```

## 한계 & 개선 포인트

- 이슈 1개씩만 처리 (멀티 이슈 큐 확장 가능)
- 코드 리뷰 없음 (자동 merge는 위험 -- PR로 멈춤)
- rate limit: OpenAI / GitHub API 둘 다 있음
