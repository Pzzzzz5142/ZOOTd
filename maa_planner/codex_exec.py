from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .util import canonical_json


class CodexExecError(RuntimeError):
    pass


MAX_PROMPT_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Preserve only what the Codex CLI needs for authentication and HTTPS. In
# particular, do not leak the desktop, Waydroid session, service internals, or
# unrelated project secrets into a diagnostic subprocess.
_PASSTHROUGH_ENV = frozenset(
    {
        "ALL_PROXY",
        "CODEX_ACCESS_TOKEN",
        "CODEX_API_KEY",
        "CODEX_CA_CERTIFICATE",
        "CODEX_HOME",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "PATH",
        "SSL_CERT_FILE",
        "USER",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)

# The model is a bounded classifier, not an execution agent. Unknown or
# removed feature flags intentionally make the subprocess fail closed after a
# Codex upgrade instead of silently gaining new capabilities.
_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "code_mode",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "multi_agent",
    "multi_agent_v2",
    "plugins",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_suggest",
    "unified_exec",
    "view_image",
)


def _timeout_seconds(
    environ: Mapping[str, str], *, variable: str, default: int
) -> int:
    raw = environ.get(variable, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise CodexExecError(f"{variable} must be an integer") from exc
    if not 1 <= value <= 300:
        raise CodexExecError(f"{variable} must be between 1 and 300")
    return value


def _codex_binary(environ: Mapping[str, str]) -> str:
    configured = environ.get("MAA_CODEX_BIN", "codex")
    if not configured or "\x00" in configured:
        raise CodexExecError("MAA_CODEX_BIN is invalid")
    candidate = shutil.which(configured, path=environ.get("PATH"))
    if candidate is None:
        raise CodexExecError(f"Codex executable was not found: {configured}")
    resolved = Path(candidate).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise CodexExecError(f"Codex executable is not runnable: {resolved}")
    return str(resolved)


def _codex_environment(environ: Mapping[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in environ.items()
        if key in _PASSTHROUGH_ENV and isinstance(value, str)
    }
    if not result.get("HOME"):
        raise CodexExecError("HOME is required for saved Codex authentication")
    result["NO_COLOR"] = "1"
    return result


def _command(
    codex: str,
    *,
    workspace: Path,
    schema_path: Path,
    model: str | None,
) -> list[str]:
    argv = [
        codex,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--cd",
        str(workspace),
        "--output-schema",
        str(schema_path),
        "--color",
        "never",
        "--config",
        'approval_policy="never"',
        "--config",
        'shell_environment_policy.inherit="none"',
    ]
    for feature in _DISABLED_FEATURES:
        argv.extend(("--disable", feature))
    if model is not None:
        argv.extend(("--model", model))
    # `-` makes stdin the entire prompt, which is stable for non-interactive
    # automation and keeps evidence separate from shell argument parsing.
    argv.append("-")
    return argv


def run_structured_codex(
    prompt: bytes,
    output_schema: Mapping[str, Any],
    *,
    timeout_variable: str,
    model_variable: str,
    default_timeout_seconds: int,
    workspace_prefix: str,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    if len(prompt) > MAX_PROMPT_BYTES:
        raise CodexExecError("Codex prompt exceeds the adapter input limit")
    source_env = os.environ if environ is None else environ
    codex = _codex_binary(source_env)
    child_env = _codex_environment(source_env)
    timeout = _timeout_seconds(
        source_env, variable=timeout_variable, default=default_timeout_seconds
    )
    model = source_env.get(model_variable) or None
    if model is not None and not _MODEL_RE.fullmatch(model):
        raise CodexExecError(f"{model_variable} is invalid")

    with tempfile.TemporaryDirectory(prefix=workspace_prefix) as raw_workspace:
        workspace = Path(raw_workspace)
        schema_path = workspace / "output-schema.json"
        schema_path.write_bytes(canonical_json(output_schema))
        schema_path.chmod(0o400)
        workspace.chmod(0o500)
        argv = _command(
            codex,
            workspace=workspace,
            schema_path=schema_path,
            model=model,
        )
        try:
            try:
                completed = runner(
                    argv,
                    input=prompt,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                    cwd=workspace,
                    env=child_env,
                    start_new_session=True,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CodexExecError(f"Codex invocation failed: {exc}") from exc
        finally:
            # TemporaryDirectory needs write permission for cleanup.
            workspace.chmod(0o700)
            if schema_path.exists():
                schema_path.chmod(0o600)

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2000:].strip()
        suffix = f": {detail}" if detail else ""
        raise CodexExecError(
            f"Codex exited with status {completed.returncode}{suffix}"
        )
    if len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise CodexExecError("Codex output exceeds the adapter limit")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexExecError("Codex did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise CodexExecError("Codex output is not a JSON object")
    return value
