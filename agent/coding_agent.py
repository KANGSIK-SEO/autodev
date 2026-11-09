"""
24/7 Coding Agent
구글 엔지니어 스타일 -- 이슈 감지 -> 코드 생성 -> PR 자동 제출
"""

import os
import json
import time
import subprocess
import requests
from datetime import datetime
from pathlib import Path

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
REPO = os.environ.get("GITHUB_REPO", "KANGSIK-SEO/autodev")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


# ── 1. 이슈 읽기 ─────────────────────────────────────────────────────────────

def fetch_open_issues():
    url = f"https://api.github.com/repos/{REPO}/issues"
    r = requests.get(url, headers=HEADERS, params={"state": "open", "per_page": 5})
    r.raise_for_status()
    return [i for i in r.json() if "pull_request" not in i]  # PR 제외


# ── 2. AI 코드 생성 ──────────────────────────────────────────────────────────

def ask_llm(prompt: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior software engineer. "
                    "Given a GitHub issue, write ONLY the Python code that solves it. "
                    "Return raw code — no markdown fences, no explanation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── 3. 브랜치 생성 + 파일 커밋 ──────────────────────────────────────────────

def get_default_branch() -> str:
    url = f"https://api.github.com/repos/{REPO}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()["default_branch"]


def get_branch_sha(branch: str) -> str:
    url = f"https://api.github.com/repos/{REPO}/git/ref/heads/{branch}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()["object"]["sha"]


def create_branch(branch: str, sha: str):
    url = f"https://api.github.com/repos/{REPO}/git/refs"
    r = requests.post(
        url,
        headers=HEADERS,
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    if r.status_code == 422:
        return  # 이미 존재
    r.raise_for_status()


def push_file(branch: str, filepath: str, content: str, message: str):
    """GitHub Contents API로 파일 1개 커밋"""
    import base64

    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}"
    # 기존 파일 SHA 확인 (없으면 신규 생성)
    existing = requests.get(url, headers=HEADERS, params={"ref": branch})
    sha = existing.json().get("sha") if existing.ok else None

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=HEADERS, json=payload)
    r.raise_for_status()


# ── 4. PR 열기 ───────────────────────────────────────────────────────────────

def open_pr(branch: str, base: str, title: str, body: str) -> str:
    url = f"https://api.github.com/repos/{REPO}/pulls"
    r = requests.post(
        url,
        headers=HEADERS,
        json={"title": title, "head": branch, "base": base, "body": body},
    )
    if r.status_code == 422:
        return "(PR already exists)"
    r.raise_for_status()
    return r.json()["html_url"]


# ── 5. 로그 ──────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    Path("logs/run.log").parent.mkdir(exist_ok=True)
    with open("logs/run.log", "a") as f:
        f.write(line + "\n")


# ── 6. 메인 루프 ─────────────────────────────────────────────────────────────

def run_once():
    log("=== agent cycle start ===")

    issues = fetch_open_issues()
    if not issues:
        log("no open issues -- idle")
        return

    issue = issues[0]
    issue_num = issue["number"]
    issue_title = issue["title"]
    issue_body = issue.get("body") or "(no description)"

    log(f"picked issue #{issue_num}: {issue_title}")

    # 이미 bot이 처리 중인지 레이블 확인
    labels = [l["name"] for l in issue.get("labels", [])]
    if "bot-processing" in labels:
        log("already processing -- skip")
        return

    # 레이블 붙이기
    requests.post(
        f"https://api.github.com/repos/{REPO}/issues/{issue_num}/labels",
        headers=HEADERS,
        json={"labels": ["bot-processing"]},
    )

    # AI 코드 생성
    prompt = f"Issue #{issue_num}: {issue_title}\n\n{issue_body}\n\nWrite Python code to solve this."
    code = ask_llm(prompt)
    log(f"generated {len(code)} chars of code")

    # 브랜치 + 파일 커밋
    default_branch = get_default_branch()
    sha = get_branch_sha(default_branch)
    branch = f"bot/issue-{issue_num}"
    create_branch(branch, sha)

    filename = f"solutions/issue_{issue_num}.py"
    header = f"# Auto-generated fix for issue #{issue_num}: {issue_title}\n# {datetime.utcnow().isoformat()}Z\n\n"
    push_file(branch, filename, header + code, f"fix: auto-solve issue #{issue_num}")

    # PR 열기
    pr_url = open_pr(
        branch=branch,
        base=default_branch,
        title=f"[bot] fix issue #{issue_num}: {issue_title}",
        body=(
            f"Closes #{issue_num}\n\n"
            f"**Auto-generated** by coding agent 🤖\n\n"
            f"```python\n{code[:800]}\n```"
        ),
    )
    log(f"PR opened: {pr_url}")

    # 이슈 코멘트
    requests.post(
        f"https://api.github.com/repos/{REPO}/issues/{issue_num}/comments",
        headers=HEADERS,
        json={"body": f"🤖 Bot opened PR: {pr_url}"},
    )
    log("=== cycle done ===")


if __name__ == "__main__":
    run_once()
