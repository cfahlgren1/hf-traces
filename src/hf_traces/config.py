from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


DEFAULT_NOTE_REF = "refs/notes/hf-traces"


@dataclass
class Config:
    bucket: str = ""
    agents: list[str] = field(default_factory=lambda: ["codex", "claude"])
    note_ref: str = DEFAULT_NOTE_REF
    git_scope: str = "global"
    dry_run: bool = False


def config_path() -> Path:
    override = os.environ.get("HF_TRACES_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "hf-traces" / "config.toml"


def load_config(path: Optional[Path] = None) -> Config:
    target = path or config_path()
    if not target.exists():
        return Config()

    raw = target.read_text(encoding="utf-8")
    data = parse_toml(raw)
    return Config(
        bucket=str(data.get("bucket", "")),
        agents=parse_agents_value(data.get("agents", ["codex", "claude"])),
        note_ref=str(data.get("note_ref", DEFAULT_NOTE_REF)),
        git_scope=str(data.get("git_scope", "global")),
        dry_run=bool(data.get("dry_run", False)),
    )


def write_config(
    config: Config, dry_run: bool = False, path: Optional[Path] = None
) -> str:
    target = path or config_path()
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_config(config), encoding="utf-8")
    return f"write config {target}"


def render_config(config: Config) -> str:
    agents = ", ".join(json.dumps(agent) for agent in config.agents)
    return "\n".join(
        [
            f"bucket = {json.dumps(config.bucket)}",
            f"agents = [{agents}]",
            f"note_ref = {json.dumps(config.note_ref)}",
            f"git_scope = {json.dumps(config.git_scope)}",
            f"dry_run = {'true' if config.dry_run else 'false'}",
            "",
        ]
    )


def parse_toml(raw: str) -> dict[str, Any]:
    if tomllib is not None:
        return dict(tomllib.loads(raw))

    data: dict[str, Any] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = parse_toml_value(value.strip())
    return data


def parse_toml_value(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("["):
        return json.loads(value)
    return json.loads(value)


def parse_agents_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [agent.strip() for agent in value.split(",") if agent.strip()]
    if isinstance(value, list):
        return [str(agent) for agent in value]
    return ["codex", "claude"]
