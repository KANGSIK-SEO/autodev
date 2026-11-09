"""
Background Coding Agent — Devin/Agent Smith 스타일
흐름: 이슈 감지 → 리포 분석 → 계획 수립 → 코드+테스트 생성 → 자가 검증 → PR
"""

import os
import json
import base64
import subprocess
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── 설정 ─────────────────────────────────────────────────────────────────────

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
REPO           = os.environ.get("GITHUB_REPO", "KANGSIK-SEO/autodev")
MAX_WORKERS    = int(os.environ.get("AGENT_WORKERS", "3"))  # 병렬 이슈 처리 수
MAX_RETRIES    = 2                                           # 테스트 실패 시 재시도

GH = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


# ── 로그 ─────────────────────────────────────────────────────────────────────

def log(issue_num: int | str, msg: str):
    ts   = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}][#{issue_num}] {msg}"
    print(line)
    Path("logs").mkdir(exist_ok=True)
    with open(f"logs/issue_{issue_num}.log", "a") as f:
        f.write(line + "\n")


# ── GitHub API helpers ────────────────────────────────────────────────────────

def gh_get(path: str, **params):
    r = requests.get(f"https://api.github.com{path}", headers=GH, params=params)
    r.raise_for_status()
    return r.json()

def gh_post(path: str, body: dict):
    r = requests.post(f"https://api.github.com{path}", headers=GH, json=body)
    r.raise_for_status()
    return r.json()

def gh_put(path: str, body: dict):
    r = requests.put(f"https://api.github.com{path}", headers=GH, json=body)
    r.raise_for_status()
    return r.json()


# ── STEP 1: 이슈 수집 ────────────────────────────────────────────────────────

def fetch_issues(limit: int = 5) -> list[dict]:
    issues = gh_get(f"/repos/{REPO}/issues", state="open", per_page=limit)
    # PR이 아닌 것, bot-processing 레이블 없는 것만
    return [
        i for i in issues
        if "pull_request" not in i
        and "bot-processing" not in [l["name"] for l in i.get("labels", [])]
    ]


# ── STEP 2: 리포 구조 파악 (백그라운드형 핵심) ───────────────────────────────

def read_repo_context(max_files: int = 20) -> str:
    """
    리포의 파일 목록 + 핵심 파일 내용을 요약.
    에이전트가 '어떤 프로젝트인지' 파악하고 기존 코드 스타일에 맞게 작성.
    """
    try:
        tree = gh_get(f"/repos/{REPO}/git/trees/HEAD", recursive=1)
        files = [
            f["path"] for f in tree.get("tree", [])
            if f["type"] == "blob"
            and f["path"].endswith((".py", ".js", ".ts", ".go", ".java"))
            and not f["path"].startswith(("node_modules", ".git", "venv", "__pycache__"))
        ][:max_files]

        context_parts = [f"=== Repo: {REPO} ===", f"Files: {', '.join(files[:30])}\n"]

        # 핵심 파일 내용 읽기 (최대 5개)
        for fpath in files[:5]:
            try:
                data = gh_get(f"/repos/{REPO}/contents/{fpath}")
                content = base64.b64decode(data["content"]).decode(errors="replace")
                context_parts.append(f"--- {fpath} ---\n{content[:800]}\n")
            except Exception:
                pass

        return "\n".join(context_parts)
    except Exception as e:
        return f"(repo context unavailable: {e})"


# ── STEP 3: AI — 계획 수립 ───────────────────────────────────────────────────

def plan_solution(issue_title: str, issue_body: str, repo_context: str) -> dict:
    """
    코드 바로 생성 X → 먼저 계획(파일명, 접근법, 테스트 방법)을 수립.
    """
    prompt = f"""
You are a senior engineer. Analyze this GitHub issue and produce a JSON plan.

REPO CONTEXT:
{repo_context[:2000]}

ISSUE: {issue_title}
{issue_body}

Return ONLY valid JSON (no markdown):
{{
  "filename": "solutions/issue_N.py",
  "approach": "one sentence plan",
  "test_cases": ["test case 1", "test case 2"],
  "complexity": "low|medium|high"
}}
"""
    raw = _llm(prompt, temperature=0.1)
    # JSON 파싱 (마크다운 fence 제거)
    raw = raw.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"filename": "solutions/issue_unknown.py", "approach": raw,
                "test_cases": [], "complexity": "medium"}


# ── STEP 4: AI — 코드 + 테스트 생성 ─────────────────────────────────────────

def generate_code_and_tests(
    issue_num: int, issue_title: str, issue_body: str,
    plan: dict, repo_context: str, prior_error: str = ""
) -> tuple[str, str]:
    """
    실패 시 prior_error를 넘겨서 재시도 → 자가 수정 루프.
    """
    retry_hint = f"\nPREVIOUS ATTEMPT FAILED:\n{prior_error}\nFix that error." if prior_error else ""

    code_prompt = f"""
You are a senior engineer. Write Python code for this issue.
Match the style of the existing repo.

REPO CONTEXT:
{repo_context[:1500]}

ISSUE #{issue_num}: {issue_title}
{issue_body}

PLAN: {plan.get('approach', '')}
{retry_hint}

Rules:
- Return ONLY raw Python code, no markdown fences
- Include all necessary imports
- Add a main() function
- Keep it under 100 lines
"""

    test_prompt = f"""
Write pytest tests for this solution.
Issue: {issue_title}
Test cases to cover: {plan.get('test_cases', [])}

Return ONLY raw Python code. Use pytest. No markdown fences.
The solution file will be imported as: from solutions.issue_{issue_num} import main
"""

    code  = _llm(code_prompt, temperature=0.2)
    tests = _llm(test_prompt, temperature=0.1)
    return code, tests


# ── STEP 5: 로컬 테스트 실행 (자가 검증) ────────────────────────────────────

def run_tests_locally(issue_num: int, code: str, tests: str) -> tuple[bool, str]:
    """
    임시 디렉토리에서 pytest 실행 → 통과 여부 + 에러 메시지 반환.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        sol_dir = Path(tmpdir) / "solutions"
        sol_dir.mkdir()
        (sol_dir / "__init__.py").write_text("")
        (sol_dir / f"issue_{issue_num}.py").write_text(code)
        (Path(tmpdir) / f"test_issue_{issue_num}.py").write_text(tests)

        result = subprocess.run(
            ["python", "-m", "pytest", f"test_issue_{issue_num}.py", "-v", "--tb=short"],
            cwd=tmpdir, capture_output=True, text=True, timeout=30
        )
        passed = result.returncode == 0
        output = result.stdout[-1500:] + result.stderr[-500:]
        return passed, output


# ── STEP 6: GitHub에 커밋 + PR ───────────────────────────────────────────────

def commit_and_pr(
    issue_num: int, issue_title: str,
    code: str, tests: str, test_output: str, plan: dict
) -> str:
    repo_info     = gh_get(f"/repos/{REPO}")
    default_branch = repo_info["default_branch"]
    sha           = gh_get(f"/repos/{REPO}/git/ref/heads/{default_branch}")["object"]["sha"]

    branch = f"bot/issue-{issue_num}"
    try:
        gh_post(f"/repos/{REPO}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": sha})
    except Exception:
        pass  # 브랜치 이미 존재

    header = (f"# Auto-generated for issue #{issue_num}: {issue_title}\n"
              f"# Generated: {datetime.utcnow().isoformat()}Z\n"
              f"# Approach: {plan.get('approach', '')}\n\n")

    _push_file(branch, plan.get("filename", f"solutions/issue_{issue_num}.py"),
               header + code, f"fix: solve issue #{issue_num}")
    _push_file(branch, f"tests/test_issue_{issue_num}.py",
               tests, f"test: add tests for issue #{issue_num}")

    pr_body = textwrap.dedent(f"""
        Closes #{issue_num}

        ## 🤖 Auto-generated by Background Agent

        **Approach:** {plan.get('approach', '')}
        **Complexity:** {plan.get('complexity', 'unknown')}
        **Tests:** {'✅ Passed' if '1 passed' in test_output or 'passed' in test_output else '⚠️ Check logs'}

        <details><summary>Test output</summary>

        ```
        {test_output[:800]}
        ```
        </details>

        <details><summary>Generated code</summary>

        ```python
        {code[:600]}
        ```
        </details>
    """).strip()

    r = requests.post(
        f"https://api.github.com/repos/{REPO}/pulls", headers=GH,
        json={"title": f"[bot] fix #{issue_num}: {issue_title}",
              "head": branch, "base": default_branch, "body": pr_body}
    )
    if r.status_code == 422:
        return "(PR already exists)"
    r.raise_for_status()
    return r.json()["html_url"]


def _push_file(branch: str, filepath: str, content: str, message: str):
    url = f"/repos/{REPO}/contents/{filepath}"
    existing = requests.get(f"https://api.github.com{url}", headers=GH,
                            params={"ref": branch})
    sha = existing.json().get("sha") if existing.ok else None
    payload = {"message": message,
               "content": base64.b64encode(content.encode()).decode(),
               "branch": branch}
    if sha:
        payload["sha"] = sha
    gh_put(url, payload)


# ── LLM 호출 ─────────────────────────────────────────────────────────────────

def _llm(prompt: str, temperature: float = 0.2) -> str:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": prompt}],
              "temperature": temperature},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── 이슈 1개 전체 처리 파이프라인 ────────────────────────────────────────────

def process_issue(issue: dict, repo_context: str):
    num   = issue["number"]
    title = issue["title"]
    body  = issue.get("body") or ""

    log(num, f"START — {title}")

    # 처리 중 레이블
    requests.post(f"https://api.github.com/repos/{REPO}/issues/{num}/labels",
                  headers=GH, json={"labels": ["bot-processing"]})

    # 계획 수립
    plan = plan_solution(title, body, repo_context)
    log(num, f"PLAN — {plan.get('approach', '?')} [{plan.get('complexity')}]")

    # 코드 생성 + 테스트 + 자가 수정 루프
    error = ""
    passed = False
    for attempt in range(MAX_RETRIES + 1):
        code, tests = generate_code_and_tests(num, title, body, plan, repo_context, error)
        log(num, f"GENERATED code={len(code)}c tests={len(tests)}c (attempt {attempt+1})")

        try:
            passed, test_output = run_tests_locally(num, code, tests)
        except Exception as e:
            passed, test_output = False, str(e)

        log(num, f"TESTS {'PASS ✅' if passed else 'FAIL ❌'}")
        if passed:
            break
        error = test_output  # 실패 메시지를 다음 시도에 전달

    # PR 제출
    pr_url = commit_and_pr(num, title, code, tests, test_output, plan)
    log(num, f"PR OPENED — {pr_url}")

    # 이슈 코멘트
    status = "✅ Tests passed" if passed else "⚠️ Tests failed (code submitted anyway)"
    requests.post(f"https://api.github.com/repos/{REPO}/issues/{num}/comments",
                  headers=GH,
                  json={"body": f"🤖 Background agent completed.\n{status}\nPR: {pr_url}"})

    # 레이블 교체
    requests.delete(f"https://api.github.com/repos/{REPO}/issues/{num}/labels/bot-processing",
                    headers=GH)
    requests.post(f"https://api.github.com/repos/{REPO}/issues/{num}/labels",
                  headers=GH, json={"labels": ["bot-done"]})

    log(num, "DONE")
    return pr_url


# ── 메인: 병렬 실행 ──────────────────────────────────────────────────────────

def main():
    log("AGENT", f"=== Background Agent start (workers={MAX_WORKERS}) ===")

    issues = fetch_issues(limit=MAX_WORKERS)
    if not issues:
        log("AGENT", "no open issues — idle")
        return

    log("AGENT", f"found {len(issues)} issue(s) — processing in parallel")

    # 리포 컨텍스트는 1번만 읽음 (모든 워커가 공유)
    repo_context = read_repo_context()
    log("AGENT", f"repo context: {len(repo_context)} chars")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_issue, issue, repo_context): issue["number"]
            for issue in issues
        }
        for future in as_completed(futures):
            num = futures[future]
            try:
                future.result()
            except Exception as e:
                log(num, f"ERROR — {e}")

    log("AGENT", "=== all done ===")


if __name__ == "__main__":
    main()
