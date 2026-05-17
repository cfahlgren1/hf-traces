# hf-traces

`hf-traces` is a small Hugging Face CLI extension for saving agent traces to a private HF bucket and leaving a Git note on the next commit.

```sh
hf extensions install cfahlgren1/hf-traces
hf traces setup --bucket <namespace>/agent-traces --agents codex,claude --git global
```

After setup:

1. Codex or Claude Code finishes a turn.
2. The agent hook uploads the session JSONL to your HF bucket.
3. The Git `post-commit` hook attaches the trace URL to the new commit with `git notes`.

The bucket stores only the raw trace JSONL. The repository stores only a small pointer like:

```txt
Agent-Trace: https://huggingface.co/buckets/<namespace>/agent-traces/tree/<repo>/<branch>/sessions/<agent>-<session>.jsonl
```

## Commands

```sh
hf traces setup --bucket cfahlgren1/agent-traces --agents codex,claude --git global
hf traces status
hf traces latest
hf traces doctor
```

Internal hook commands are installed by `setup`:

```sh
hf traces hook codex stop
hf traces hook claude stop
hf traces hook git post-commit
```

## Git notes

Trace links are written to `refs/notes/hf-traces`.

Git notes are local refs. They do not travel with a normal branch push unless you explicitly push or fetch the notes ref. This extension does not push notes automatically.

```sh
git notes --ref=hf-traces show HEAD
git log --show-notes=hf-traces
```

## Buckets

Create the bucket before the first upload:

```sh
hf buckets create <namespace>/agent-traces --private --exist-ok
```

The upload path is:

```txt
hf://buckets/<namespace>/agent-traces/<repo>/<branch>/sessions/<agent>-<session>.jsonl
```

