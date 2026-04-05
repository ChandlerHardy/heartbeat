#!/usr/bin/env python3
"""ch-code-reviewer — GitHub App webhook handler for automated PR reviews."""

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import jwt
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Config
APP_ID = 3127694
PEM_PATH = os.path.expanduser("~/code-reviewer/ch-code-reviewer.pem")
WEBHOOK_SECRET_PATH = os.path.expanduser("~/code-reviewer/webhook-secret.txt")
REPOS_DIR = "/mnt/block_volume/repos"
MAX_FIX_ATTEMPTS = 3

# Repo name mapping — GitHub repo name -> local directory name
REPO_NAME_MAP = {
    "gnomestead": "gnomestead-ios",
}
LOG_PREFIX = "[code-reviewer]"


def log(msg):
    print(f"{LOG_PREFIX} {msg}", flush=True)


def get_webhook_secret():
    try:
        return Path(WEBHOOK_SECRET_PATH).read_text().strip()
    except FileNotFoundError:
        return None


def verify_signature(payload, signature):
    secret = get_webhook_secret()
    if not secret:
        log("WARNING: No webhook secret configured, skipping verification")
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def generate_jwt():
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": str(APP_ID),
    }
    pem = Path(PEM_PATH).read_text()
    return jwt.encode(payload, pem, algorithm="RS256")


def get_installation_token(installation_id):
    token = generate_jwt()
    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    return resp.json()["token"]


def get_pr_diff(owner, repo, pr_number, token):
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    resp.raise_for_status()
    return resp.text


def parse_verdict(review_text):
    first_lines = review_text[:500].upper()
    if "NEEDS CHANGES" in first_lines or "NEEDS_CHANGES" in first_lines:
        return "REQUEST_CHANGES"
    return "APPROVE"


def post_review(owner, repo, pr_number, body, token):
    """Post a formal PR review (APPROVE or REQUEST_CHANGES)."""
    verdict = parse_verdict(body)
    resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"body": body, "event": verdict},
    )
    resp.raise_for_status()
    log(f"Posted {verdict} review on {owner}/{repo}#{pr_number}")
    return verdict


def post_comment(owner, repo, pr_number, body, token):
    """Post a regular issue comment (for fix attempt tracking)."""
    requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"body": body},
    )


def get_fix_attempt_count(owner, repo, pr_number, token):
    """Count [auto-fix] comments on the PR to track attempts."""
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params={"per_page": 100},
    )
    resp.raise_for_status()
    return sum(1 for c in resp.json() if "[auto-fix" in c.get("body", ""))


def write_temp(content, suffix=".txt"):
    """Write content to a temp file, return path. Caller must unlink."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name


def run_claude(repo_path, prompt, system_prompt=None, max_turns=25, timeout=300):
    """Run claude -p in a repo directory with file-based prompt/system prompt.

    Writes all dynamic text to temp files to avoid shell quoting issues.
    Returns (stdout, stderr, returncode).
    """
    prompt_file = write_temp(prompt)
    cleanup = [prompt_file]

    cmd = f"cd '{repo_path}' && cat '{prompt_file}' | claude -p --dangerously-skip-permissions --max-turns {max_turns}"

    if system_prompt:
        sys_file = write_temp(system_prompt)
        cleanup.append(sys_file)
        # Use double-quoted command substitution to avoid shell quoting issues
        cmd += f" --append-system-prompt \"$(cat '{sys_file}')\""

    try:
        result = subprocess.run(
            ["bash", "-l", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    finally:
        for f in cleanup:
            try:
                os.unlink(f)
            except OSError:
                pass


def fix_pr(owner, repo, pr_number, head_ref, installation_id, review_feedback, diff):
    """Auto-fix a PR based on review feedback. Runs claude -p to apply fixes."""
    try:
        token = get_installation_token(installation_id)

        # Check attempt count
        attempts = get_fix_attempt_count(owner, repo, pr_number, token)
        if attempts >= MAX_FIX_ATTEMPTS:
            log(f"Max fix attempts ({MAX_FIX_ATTEMPTS}) reached for {owner}/{repo}#{pr_number}")
            post_comment(
                owner, repo, pr_number,
                f"**[auto-fix]** Max attempts ({MAX_FIX_ATTEMPTS}) reached. "
                "This PR needs manual intervention.",
                token,
            )
            return

        attempt_num = attempts + 1
        log(f"Auto-fix attempt {attempt_num}/{MAX_FIX_ATTEMPTS} for {owner}/{repo}#{pr_number}")

        local_name = REPO_NAME_MAP.get(repo, repo)
        repo_path = f"{REPOS_DIR}/{local_name}"

        # Truncate diff for context (keep it focused)
        diff_context = diff[:30000] if diff else "(no diff available)"

        fix_prompt = (
            f"A code reviewer flagged issues on branch {head_ref}. "
            f"Fix ALL of the following issues, then commit and push.\n\n"
            f"## Review Feedback\n\n{review_feedback}\n\n"
            f"## Current PR Diff (for reference)\n\n```diff\n{diff_context}\n```\n\n"
            f"## Instructions\n\n"
            f"1. `git checkout {head_ref} && git pull origin {head_ref}`\n"
            f"2. BEFORE editing, investigate the codebase:\n"
            f"   - Use jcodemunch search_symbols to find existing types, functions, exports\n"
            f"   - Use context7 to check framework docs before changing build config\n"
            f"   - Read the files around the changes to match conventions\n"
            f"3. Fix every issue the reviewer flagged — do not skip any\n"
            f"4. If the reviewer says a function/type is missing, search first. "
            f"If it truly doesn't exist, create it matching existing patterns.\n"
            f"5. If the reviewer says to revert something, revert it exactly\n"
            f"6. `git add` changed files, commit: "
            f"'fix: address review feedback (auto-fix {attempt_num}/{MAX_FIX_ATTEMPTS})'\n"
            f"7. `git push origin {head_ref}`\n"
            f"8. Do NOT create new branches. Push to the existing branch.\n"
        )

        fix_system = (
            "You are an autonomous code fixer. You have MCP tools — USE THEM:\n"
            "- jcodemunch: search_symbols, get_file_outline, get_file_tree, find_references\n"
            "- context7: resolve-library-id + query-docs for framework documentation\n"
            "- codebase-memory-mcp: search_code, get_architecture\n\n"
            "RULES:\n"
            "- ALWAYS search the codebase before editing. Never guess at types or signatures.\n"
            "- If a function doesn't exist, check similar ones to base yours on.\n"
            "- If told to revert a config change, check the framework docs to confirm the correct value.\n"
            "- Read the PR diff to understand what was changed and what context you're working in.\n"
            "- After committing, verify with a quick build check if possible (e.g. npx tsc --noEmit)."
        )

        stdout, stderr, rc = run_claude(
            repo_path, fix_prompt, fix_system,
            max_turns=40, timeout=600,
        )

        success = rc == 0 and stdout

        if success:
            summary = stdout[:500] + ("..." if len(stdout) > 500 else "")
            post_comment(
                owner, repo, pr_number,
                f"**[auto-fix {attempt_num}/{MAX_FIX_ATTEMPTS}]** "
                f"Applied fixes based on review feedback.\n\n"
                f"<details><summary>Claude output</summary>\n\n"
                f"```\n{summary}\n```\n</details>",
                token,
            )
            log(f"Auto-fix {attempt_num} pushed for {owner}/{repo}#{pr_number}")
        else:
            err = stderr[:300] if stderr else "(no output)"
            post_comment(
                owner, repo, pr_number,
                f"**[auto-fix {attempt_num}/{MAX_FIX_ATTEMPTS}]** "
                f"Fix attempt failed.\n\n"
                f"<details><summary>Error</summary>\n\n"
                f"```\n{err}\n```\n</details>",
                token,
            )
            log(f"Auto-fix {attempt_num} failed for {owner}/{repo}#{pr_number}: {err[:100]}")

    except Exception as e:
        log(f"Error in fix_pr for {owner}/{repo}#{pr_number}: {e}")


def review_pr(owner, repo, pr_number, installation_id, head_ref):
    """Run claude -p review and post as bot. Runs in background thread."""
    try:
        log(f"Reviewing {owner}/{repo}#{pr_number} (branch: {head_ref})")

        token = get_installation_token(installation_id)
        diff = get_pr_diff(owner, repo, pr_number, token)

        if not diff.strip():
            log(f"Empty diff for {owner}/{repo}#{pr_number}, skipping")
            return

        # Truncate very large diffs for review
        review_diff = diff[:50000] + ("\n\n... (diff truncated at 50KB)" if len(diff) > 50000 else "")

        local_name = REPO_NAME_MAP.get(repo, repo)
        repo_path = f"{REPOS_DIR}/{local_name}"

        review_prompt = (
            f"Review this PR diff (branch: {head_ref}). Be concise (under 300 words). Check for:\n"
            "1. Bugs or logic errors\n"
            "2. Missed edge cases\n"
            "3. Style inconsistencies with surrounding code\n"
            "4. Functional completeness — does the PR actually work as-is?\n\n"
            "Verdict rules:\n"
            "- NEEDS CHANGES if: the PR has bugs, missing files required for it to work "
            "(e.g. DB migration for schema changes, missing imports, broken build), "
            "or would fail at runtime.\n"
            "- LGTM if: the code is correct and functional. Non-blocking observations "
            "(design tradeoffs, future considerations) are fine under LGTM.\n"
            "- The test: 'If I merge this right now, does it work?' If no → NEEDS CHANGES.\n\n"
            "Format: Start with a verdict (LGTM or NEEDS CHANGES), then bullet points.\n"
            "If LGTM, say so briefly. Do not pad with praise.\n\n"
            f"Diff:\n{review_diff}"
        )

        review_system = (
            "You are a code reviewer. You have MCP tools — use them to understand context:\n"
            "- jcodemunch: search_symbols, get_file_outline to check existing code\n"
            "- context7: resolve-library-id + query-docs for framework API verification\n"
            "- codebase-memory-mcp: search_code, get_architecture for project conventions\n\n"
            "Review quality rules:\n"
            "- Check that new code matches existing patterns and conventions in the repo.\n"
            "- Verify error handling is consistent with the rest of the codebase.\n"
            "- Flag any changes that could break existing functionality.\n"
            "- Do not nitpick style — focus on correctness and maintainability.\n"
            "- If unsure whether a type/function exists, USE jcodemunch to search before flagging."
        )

        stdout, stderr, rc = run_claude(
            repo_path, review_prompt, review_system,
            max_turns=25, timeout=300,
        )

        if not stdout:
            log(f"Empty review output for {owner}/{repo}#{pr_number}")
            return

        body = f"## Automated Review\n\n{stdout}\n\n---\n*Reviewed by ch-code-reviewer*"
        verdict = post_review(owner, repo, pr_number, body, token)

        # If changes requested, auto-trigger fix cycle with the diff for context
        if verdict == "REQUEST_CHANGES":
            log(f"Triggering auto-fix for {owner}/{repo}#{pr_number}")
            fix_pr(owner, repo, pr_number, head_ref, installation_id, stdout, diff)

    except Exception as e:
        log(f"Error reviewing {owner}/{repo}#{pr_number}: {e}")


@app.route("/webhook/code-reviewer", methods=["POST"])
def webhook():
    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(payload, signature):
        log("Signature verification failed")
        return jsonify({"error": "Invalid signature"}), 403

    event = request.headers.get("X-GitHub-Event")
    if event != "pull_request":
        log(f"Ignored event: {event}")
        return jsonify({"status": "ignored", "event": event}), 200

    data = request.get_json()
    action = data.get("action")

    # Only review on PR open or synchronize (new push)
    if action not in ("opened", "synchronize"):
        log(f"Ignored action: {action}")
        return jsonify({"status": "ignored", "action": action}), 200

    pr = data["pull_request"]
    head_ref = pr["head"]["ref"]

    # Only review heartbeat branches
    if not head_ref.startswith("heartbeat/"):
        log(f"Ignored branch: {head_ref}")
        return jsonify({"status": "ignored", "reason": "not a heartbeat branch"}), 200

    owner = data["repository"]["owner"]["login"]
    repo = data["repository"]["name"]
    pr_number = pr["number"]
    installation_id = data["installation"]["id"]

    # Run review in background so we respond to GitHub quickly
    thread = threading.Thread(
        target=review_pr,
        args=(owner, repo, pr_number, installation_id, head_ref),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "review_queued", "pr": pr_number}), 200


@app.route("/webhook/code-reviewer", methods=["GET"])
def health():
    pem_exists = Path(PEM_PATH).exists()
    return jsonify({
        "status": "running",
        "app_id": APP_ID,
        "pem_configured": pem_exists,
        "max_fix_attempts": MAX_FIX_ATTEMPTS,
    })


if __name__ == "__main__":
    log("Starting ch-code-reviewer webhook handler on port 9100")
    app.run(host="127.0.0.1", port=9100)
