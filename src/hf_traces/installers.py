from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


CODEX_HOOK_PATH = Path.home() / ".codex" / "hooks.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
MANAGED_START = "# >>> hf-traces"
MANAGED_END = "# <<< hf-traces"


def hook_entrypoint() -> str:
    argv0 = Path(sys.argv[0] or "").resolve()
    if argv0.name in {"hf-traces", "hf-traces.exe"} and argv0.exists():
        return str(argv0)

    on_path = shutil.which("hf-traces")
    if on_path:
        return on_path

    return "hf traces"


def hook_command(*args: str) -> str:
    entrypoint = hook_entrypoint()
    if entrypoint.startswith("/"):
        return " ".join([shlex.quote(entrypoint), *args])
    return " ".join([entrypoint, *args])


def codex_stop_command() -> str:
    return hook_command("hook", "codex", "stop")


def claude_stop_command() -> str:
    return hook_command("hook", "claude", "stop")


def git_post_commit_command() -> str:
    return hook_command("hook", "git", "post-commit")


def install_codex_hook(dry_run: bool = False) -> str:
    return install_json_hook(
        CODEX_HOOK_PATH, "Stop", codex_stop_command(), dry_run=dry_run
    )


def install_claude_hook(dry_run: bool = False) -> str:
    return install_json_hook(
        CLAUDE_SETTINGS_PATH, "Stop", claude_stop_command(), dry_run=dry_run
    )


def uninstall_codex_hook(dry_run: bool = False) -> str:
    return uninstall_json_hook(CODEX_HOOK_PATH, "hook codex stop", dry_run=dry_run)


def uninstall_claude_hook(dry_run: bool = False) -> str:
    return uninstall_json_hook(
        CLAUDE_SETTINGS_PATH, "hook claude stop", dry_run=dry_run
    )


def install_json_hook(
    path: Path, event: str, command: str, dry_run: bool = False
) -> str:
    suffix = hook_suffix(command)
    data = read_json_file(path)
    if json_has_command(data, command):
        return f"keep existing hook {path}"

    remove_json_command(data, suffix)

    data.setdefault("hooks", {}).setdefault(event, []).append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ]
        }
    )

    if not dry_run:
        write_json_file(path, data)
    return f"install hook {path}"


def uninstall_json_hook(path: Path, suffix: str, dry_run: bool = False) -> str:
    data = read_json_file(path)
    changed = remove_json_command(data, suffix)
    if changed and not dry_run:
        write_json_file(path, data)
    action = "remove hook" if changed else "keep missing hook"
    return f"{action} {path}"


def hook_suffix(command: str) -> str:
    marker = " hook "
    index = command.find(marker)
    if index == -1:
        return command
    return command[index + 1 :]


def install_git_hook(scope: str, note_ref: str, dry_run: bool = False) -> list[str]:
    if scope == "global":
        hook_path, created_global_path = global_post_commit_path()
        script = global_dispatcher_script()
    elif scope == "local":
        hook_path = local_post_commit_path()
        created_global_path = False
        script = "#!/usr/bin/env sh\nset -eu\n"
    else:
        return []

    results = []
    results.append(
        install_managed_block(hook_path, managed_git_block(), script, dry_run=dry_run)
    )

    if scope == "global" and created_global_path:
        hooks_dir = str(hook_path.parent)
        if not dry_run:
            run(["git", "config", "--global", "core.hooksPath", hooks_dir])
        results.append(f"set global core.hooksPath {hooks_dir}")

    if scope == "global":
        effective_hook_path = effective_post_commit_path()
        if effective_hook_path and effective_hook_path != hook_path:
            results.append(
                install_managed_block(
                    effective_hook_path,
                    managed_git_block(),
                    "#!/usr/bin/env sh\nset -eu\n",
                    dry_run=dry_run,
                )
            )
    elif scope == "local":
        configured_path = run(
            ["git", "config", "--get", "core.hooksPath"], check=False
        ).stdout.strip()
        if configured_path and resolve_hooks_path(configured_path) != hook_path.parent:
            if not dry_run:
                run(
                    [
                        "git",
                        "config",
                        "--local",
                        "core.hooksPath",
                        str(hook_path.parent),
                    ]
                )
            results.append(f"set local core.hooksPath {hook_path.parent}")

    config_args = ["git", "config"]
    if scope == "global":
        config_args.append("--global")
    config_args.extend(["--get-all", "notes.displayRef"])
    current_refs = run(config_args, check=False).stdout.splitlines()
    if note_ref not in current_refs:
        set_args = ["git", "config"]
        if scope == "global":
            set_args.append("--global")
        set_args.extend(["--add", "notes.displayRef", note_ref])
        if not dry_run:
            run(set_args)
        results.append(f"add notes.displayRef {note_ref}")
    else:
        results.append(f"keep notes.displayRef {note_ref}")

    return results


def uninstall_git_hook(scope: str, dry_run: bool = False) -> str:
    if scope == "global":
        hook_path, _created_global_path = global_post_commit_path()
    elif scope == "local":
        hook_path = local_post_commit_path()
    else:
        return "skip git hook"

    changed = remove_managed_block(hook_path, dry_run=dry_run)
    if scope == "local":
        configured_path = run(
            ["git", "config", "--local", "--get", "core.hooksPath"], check=False
        ).stdout.strip()
        if (
            configured_path
            and resolve_hooks_path(configured_path) == hook_path.parent
            and not dry_run
        ):
            run(["git", "config", "--local", "--unset", "core.hooksPath"], check=False)
    action = "remove git hook block" if changed else "keep missing git hook block"
    return f"{action} {hook_path}"


def check_status(note_ref: str, git_scope: str = "local") -> dict[str, bool]:
    status = {
        "codex stop hook": json_has_command(
            read_json_file(CODEX_HOOK_PATH), codex_stop_command()
        ),
        "claude stop hook": json_has_command(
            read_json_file(CLAUDE_SETTINGS_PATH), claude_stop_command()
        ),
    }

    if git_scope == "local":
        try:
            hook_path = local_post_commit_path()
        except subprocess.CalledProcessError:
            hook_path = None
        refs = run(["git", "config", "--get-all", "notes.displayRef"], check=False)
    else:
        hook_path, _created_global_path = global_post_commit_path()
        effective_hook_path = effective_post_commit_path()
        if effective_hook_path and effective_hook_path != hook_path:
            hook_path = effective_hook_path
        refs = run(
            ["git", "config", "--global", "--get-all", "notes.displayRef"],
            check=False,
        )

    status["git post-commit hook"] = (
        file_has_managed_block(hook_path) if hook_path else False
    )
    status["git notes display ref"] = note_ref in refs.stdout.splitlines()
    return status


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def json_has_command(data: dict[str, Any], command: str) -> bool:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return False

    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for hook in group.get("hooks", []):
                if hook.get("command") == command:
                    return True
    return False


def remove_json_command(data: dict[str, Any], suffix: str) -> bool:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return False

    changed = False
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        next_groups = []
        for group in groups:
            group_hooks = group.get("hooks", [])
            next_hooks = [
                hook
                for hook in group_hooks
                if not str(hook.get("command", "")).endswith(suffix)
            ]
            if len(next_hooks) != len(group_hooks):
                changed = True
            if next_hooks:
                group["hooks"] = next_hooks
                next_groups.append(group)
        if next_groups:
            hooks[event] = next_groups
        else:
            hooks.pop(event, None)
    return changed


def global_post_commit_path() -> tuple[Path, bool]:
    configured = run(
        ["git", "config", "--global", "--get", "core.hooksPath"], check=False
    ).stdout.strip()
    if configured:
        return Path(os.path.expanduser(configured)) / "post-commit", False
    return Path.home() / ".config" / "hf-traces" / "git-hooks" / "post-commit", True


def local_post_commit_path() -> Path:
    return local_hooks_dir() / "post-commit"


def local_hooks_dir() -> Path:
    result = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return Path(result.stdout.strip()) / "hooks"


def resolve_hooks_path(value: str) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        root = run(["git", "rev-parse", "--show-toplevel"], check=False).stdout.strip()
        if root:
            path = Path(root) / path
    return path.resolve()


def effective_post_commit_path() -> Optional[Path]:
    result = run(["git", "config", "--get", "core.hooksPath"], check=False)
    configured = result.stdout.strip()
    if not configured:
        return None

    path = Path(os.path.expanduser(configured))
    if not path.is_absolute():
        root = run(["git", "rev-parse", "--show-toplevel"], check=False).stdout.strip()
        if root:
            path = Path(root) / path
    return path / "post-commit"


def install_managed_block(
    path: Path, block: str, default_script: str, dry_run: bool = False
) -> str:
    current = path.read_text(encoding="utf-8") if path.exists() else default_script
    next_content = replace_managed_block(current, block)

    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(next_content, encoding="utf-8")
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"install git hook block {path}"


def remove_managed_block(path: Path, dry_run: bool = False) -> bool:
    if not path.exists():
        return False
    current = path.read_text(encoding="utf-8")
    next_content = strip_managed_block(current)
    if next_content == current:
        return False
    if not dry_run:
        path.write_text(next_content, encoding="utf-8")
    return True


def replace_managed_block(current: str, block: str) -> str:
    stripped = strip_managed_block(current).rstrip()
    if not stripped:
        return f"{block}\n"

    lines = stripped.splitlines()
    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        rest = "\n".join(lines[1:]).strip()
        if rest:
            return f"{shebang}\n\n{block}\n\n{rest}\n"
        return f"{shebang}\n\n{block}\n"

    return f"{block}\n\n{stripped}\n"


def strip_managed_block(current: str) -> str:
    if MANAGED_START not in current or MANAGED_END not in current:
        return current

    before, rest = current.split(MANAGED_START, 1)
    _managed, after = rest.split(MANAGED_END, 1)
    return (before.rstrip() + "\n" + after.lstrip()).lstrip("\n")


def file_has_managed_block(path: Path) -> bool:
    return path.exists() and MANAGED_START in path.read_text(encoding="utf-8")


def managed_git_block() -> str:
    command = git_post_commit_command()
    entrypoint = hook_entrypoint()
    guard = (
        f"[ -x {shlex.quote(entrypoint)} ]"
        if entrypoint.startswith("/")
        else "command -v hf >/dev/null 2>&1"
    )

    return "\n".join(
        [
            MANAGED_START,
            f"if {guard}; then",
            f"  {command}",
            "fi",
            MANAGED_END,
        ]
    )


def global_dispatcher_script() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env sh",
            "set -eu",
            "",
            "repo_hook=$(git rev-parse --path-format=absolute --git-path hooks/post-commit 2>/dev/null || true)",
            'if [ -n "$repo_hook" ] && [ -x "$repo_hook" ] && [ "$repo_hook" != "$0" ]; then',
            '  "$repo_hook" "$@"',
            "fi",
            "",
        ]
    )


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)
