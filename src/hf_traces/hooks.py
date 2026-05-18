from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .config import Config, load_config


def handle_agent_record(agent: str) -> int:
    try:
        payload = read_stdin_json()
        config = load_config()
        state_context = state_from_payload(config, agent, payload)
        if state_context is None:
            return 0

        repo, state = state_context
        write_state(repo.state_path, preserve_existing_note_state(repo, state))
    except Exception as error:  # pragma: no cover - hooks should never break the agent
        warn(str(error))
    return 0


def handle_agent_stop(agent: str) -> int:
    try:
        payload = read_stdin_json()
        config = load_config()
        state_context = state_from_payload(config, agent, payload)
        if state_context is None:
            return 0

        repo, state = state_context
        transcript_path = Path(str(state["trace_path"])).expanduser()
        if not transcript_path.exists():
            warn(f"trace file does not exist: {transcript_path}")
            return 0

        session_id = str(state.get("session_id") or transcript_path.stem)
        state = publish_trace(config, repo, transcript_path, agent, session_id)
        if state:
            write_state(repo.state_path, preserve_existing_note_state(repo, state))
    except Exception as error:  # pragma: no cover - hooks should never break the agent
        warn(str(error))
    return 0


def handle_git_post_commit() -> int:
    try:
        config = load_config()
        repo = repo_context(Path.cwd())
        if repo is None:
            return 0

        head = git(["rev-parse", "HEAD"], repo.root).stdout.strip()
        state = read_state(repo.state_path)
        if not state:
            state = discover_codex_state(config, repo)
            if state:
                write_state(repo.state_path, state)

        if not state:
            return 0
        if state.get("consumed") and state.get("noted_commit") == head:
            return 0

        if not state.get("uploaded"):
            state = publish_state_trace(config, repo, state)
            if not state:
                return 0
            write_state(repo.state_path, state)

        trace_url = state.get("trace_url")
        if not trace_url:
            return 0

        note_message = f"Agent-Trace: {trace_url}"
        existing_note = git(
            ["notes", f"--ref={config.note_ref}", "show", "HEAD"],
            repo.root,
            check=False,
        )
        if trace_url not in existing_note.stdout:
            git(
                [
                    "notes",
                    f"--ref={config.note_ref}",
                    "append",
                    "-m",
                    note_message,
                    "HEAD",
                ],
                repo.root,
            )

        state["consumed"] = True
        state["noted_commit"] = head
        state["noted_at"] = now_iso()
        write_state(repo.state_path, state)
    except Exception as error:  # pragma: no cover - post-commit should stay best-effort
        warn(str(error))
    return 0


def latest_state(cwd: Optional[Path] = None) -> Optional[dict[str, object]]:
    repo = repo_context(cwd or Path.cwd())
    if repo is None:
        return None
    return read_state(repo.state_path)


def publish_trace(
    config: Config,
    repo: "RepoContext",
    transcript_path: Path,
    agent: str,
    session_id: str,
) -> Optional[dict[str, object]]:
    bucket = normalize_bucket(config.bucket)
    bucket_path = bucket_path_for(repo, agent, session_id)
    destination = f"hf://buckets/{bucket}/{bucket_path}"
    dry_run = config.dry_run or os.environ.get("HF_TRACES_DRY_RUN") == "1"

    if not dry_run:
        result = run_command(["hf", "buckets", "cp", str(transcript_path), destination])
        if result.returncode != 0:
            warn(result.stderr.strip() or "hf bucket upload failed")
            return None

    return build_state(
        config,
        repo,
        transcript_path,
        agent,
        session_id,
        bucket,
        bucket_path,
        uploaded=not dry_run,
    )


def publish_state_trace(
    config: Config, repo: "RepoContext", state: dict[str, object]
) -> Optional[dict[str, object]]:
    trace_path = Path(str(state.get("trace_path") or "")).expanduser()
    if not trace_path.exists():
        warn(f"trace file does not exist: {trace_path}")
        return None

    agent = str(state.get("agent") or "codex")
    session_id = str(state.get("session_id") or trace_path.stem)
    return publish_trace(config, repo, trace_path, agent, session_id)


def state_from_payload(
    config: Config, agent: str, payload: dict[str, object]
) -> Optional[tuple["RepoContext", dict[str, object]]]:
    if agent not in config.agents or not config.bucket:
        return None

    try:
        transcript_path = transcript_from_payload(payload)
    except ValueError:
        return None

    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser()
    session_id = str(payload.get("session_id") or transcript_path.stem)
    repo = repo_context(cwd)
    if repo is None:
        return None

    bucket = normalize_bucket(config.bucket)
    bucket_path = bucket_path_for(repo, agent, session_id)
    state = build_state(
        config,
        repo,
        transcript_path,
        agent,
        session_id,
        bucket,
        bucket_path,
        uploaded=False,
    )
    return repo, state


def discover_codex_state(
    config: Config, repo: "RepoContext"
) -> Optional[dict[str, object]]:
    if "codex" not in config.agents:
        return None

    for trace_path in recent_codex_session_paths():
        session_meta = read_codex_session_meta(trace_path)
        if not session_meta:
            continue

        cwd_value = session_meta.get("cwd")
        if not cwd_value or not path_is_inside_repo(Path(str(cwd_value)), repo.root):
            continue

        session_id = str(session_meta.get("id") or trace_path.stem)
        return publish_trace(config, repo, trace_path, "codex", session_id)

    return None


def recent_codex_session_paths(max_age_seconds: int = 24 * 60 * 60) -> list[Path]:
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return []

    cutoff = time.time() - max_age_seconds
    candidates = []
    for path in sessions_root.rglob("*.jsonl"):
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        if modified_at >= cutoff:
            candidates.append((modified_at, path))

    return [path for _modified_at, path in sorted(candidates, reverse=True)]


def read_codex_session_meta(path: Path) -> Optional[dict[str, object]]:
    try:
        with path.open("r", encoding="utf-8") as trace_file:
            row = json.loads(trace_file.readline())
    except (OSError, json.JSONDecodeError):
        return None

    if row.get("type") != "session_meta":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload


def path_is_inside_repo(path: Path, repo_root: Path) -> bool:
    try:
        resolved_path = path.expanduser().resolve(strict=False)
        resolved_root = repo_root.resolve(strict=False)
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def preserve_existing_note_state(
    repo: "RepoContext", state: dict[str, object]
) -> dict[str, object]:
    existing = read_state(repo.state_path)
    if not existing or not existing.get("consumed"):
        return state
    if existing.get("session_id") != state.get("session_id"):
        return state

    state["consumed"] = True
    for key in ("noted_commit", "noted_at"):
        if key in existing:
            state[key] = existing[key]
    return state


def bucket_path_for(repo: "RepoContext", agent: str, session_id: str) -> str:
    return "/".join(
        [
            repo.name,
            repo.branch,
            "sessions",
            f"{agent}-{safe_path_component(session_id)}.jsonl",
        ]
    )


def build_state(
    config: Config,
    repo: "RepoContext",
    transcript_path: Path,
    agent: str,
    session_id: str,
    bucket: str,
    bucket_path: str,
    uploaded: bool,
) -> dict[str, object]:
    return {
        "agent": agent,
        "session_id": session_id,
        "trace_path": str(transcript_path),
        "trace_url": trace_url(bucket, bucket_path),
        "bucket": bucket,
        "bucket_path": bucket_path,
        "repo": repo.name,
        "branch": repo.branch,
        "cwd": str(repo.root),
        "note_ref": config.note_ref,
        "uploaded": uploaded,
        "consumed": False,
        "created_at": now_iso(),
    }


def read_stdin_json() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def transcript_from_payload(payload: dict[str, object]) -> Path:
    value = (
        payload.get("transcript_path")
        or payload.get("transcriptPath")
        or payload.get("conversation_log_path")
        or payload.get("trace_path")
    )
    if not value:
        raise ValueError("hook payload did not include transcript_path")
    return Path(str(value)).expanduser()


class RepoContext:
    def __init__(self, root: Path, state_path: Path, name: str, branch: str) -> None:
        self.root = root
        self.state_path = state_path
        self.name = name
        self.branch = branch


def repo_context(cwd: Path) -> Optional[RepoContext]:
    root_result = git(["rev-parse", "--show-toplevel"], cwd, check=False)
    if root_result.returncode != 0:
        return None

    root = Path(root_result.stdout.strip())
    state_result = git(
        ["rev-parse", "--path-format=absolute", "--git-path", "hf-traces/state.json"],
        root,
    )
    state_path = Path(state_result.stdout.strip())
    return RepoContext(
        root=root, state_path=state_path, name=repo_name(root), branch=branch_name(root)
    )


def repo_name(root: Path) -> str:
    result = git(["remote", "get-url", "origin"], root, check=False)
    if result.returncode != 0:
        return safe_path_component(root.name)

    remote = result.stdout.strip().removesuffix(".git")
    if "/" not in remote:
        return safe_path_component(root.name)
    return safe_path_component(remote.rsplit("/", 1)[-1])


def branch_name(root: Path) -> str:
    result = git(["branch", "--show-current"], root, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    result = git(["rev-parse", "--short", "HEAD"], root, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "detached"


def normalize_bucket(bucket: str) -> str:
    return bucket.removeprefix("hf://buckets/").strip("/")


def trace_url(bucket: str, bucket_path: str) -> str:
    return f"https://huggingface.co/buckets/{quote(bucket, safe='/')}/tree/{quote(bucket_path, safe='/-_.~')}"


def safe_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "unknown"


def read_state(path: Path) -> Optional[dict[str, object]]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def git(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_command(["git", "-C", str(cwd), *args], check=check)


def run_command(
    args: list[str], check: bool = False
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(args)}")
    return result


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def warn(message: str) -> None:
    if message:
        print(f"hf-traces: {message}", file=sys.stderr)
