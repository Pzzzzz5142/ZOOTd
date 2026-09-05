from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

from .util import atomic_write_json


class CodexSDKError(RuntimeError):
    pass


MAX_PROMPT_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENVIRONMENT_LOCK = threading.Lock()

# Preserve only what the Codex SDK runtime needs for authentication and HTTPS.
# In particular, do not leak the desktop, Waydroid session, service internals,
# or unrelated project secrets into the read-only diagnostic runtime.
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

# These adapters are bounded classifiers, not execution agents. The pinned SDK
# runtime recognizes every feature key in this list. Keeping the list explicit
# makes a dependency upgrade reviewable and prevents user configuration from
# silently adding unrelated capabilities.
READ_ONLY_DISABLED_FEATURES = (
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


SandboxName = Literal["read-only", "workspace-write", "full-access"]


@dataclass(frozen=True, slots=True)
class CodexSDKRequest:
    prompt: str
    output_schema: dict[str, Any]
    cwd: Path
    sandbox: SandboxName
    images: tuple[Path, ...]
    model: str | None
    timeout_seconds: int
    environment: dict[str, str]
    config_overrides: tuple[str, ...]
    ephemeral: bool = True
    approval_mode: Literal["deny-all"] = "deny-all"
    resume_thread_id: str | None = None
    thread_id_path: Path | None = None
    thread_state_context: dict[str, str] | None = None


CodexSDKRunner = Callable[[CodexSDKRequest], str]


def feature_overrides(disabled_features: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"features.{feature}=false" for feature in disabled_features)


def _timeout_seconds(
    environ: Mapping[str, str], *, variable: str, default: int
) -> int:
    raw = environ.get(variable, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise CodexSDKError(f"{variable} must be an integer") from exc
    if not 1 <= value <= 300:
        raise CodexSDKError(f"{variable} must be between 1 and 300")
    return value


def _model(environ: Mapping[str, str], *, variable: str) -> str | None:
    model = environ.get(variable) or None
    if model is not None and not _MODEL_RE.fullmatch(model):
        raise CodexSDKError(f"{variable} is invalid")
    return model


def diagnostic_environment(environ: Mapping[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in environ.items()
        if key in _PASSTHROUGH_ENV and isinstance(value, str)
    }
    if not result.get("HOME"):
        raise CodexSDKError("HOME is required for saved Codex authentication")
    result["NO_COLOR"] = "1"
    return result


@contextmanager
def _isolated_environment(environment: Mapping[str, str]) -> Iterator[None]:
    """Give the SDK-launched runtime an exact, rather than additive, env."""

    with _ENVIRONMENT_LOCK:
        original = dict(os.environ)
        os.environ.clear()
        os.environ.update(environment)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)


async def _invoke_sdk(request: CodexSDKRequest) -> str:
    try:
        from openai_codex import (
            ApprovalMode,
            AsyncCodex,
            CodexConfig,
            LocalImageInput,
            Sandbox,
            TextInput,
        )
    except ImportError as exc:
        raise CodexSDKError(
            "the openai-codex SDK is unavailable; run scripts/bootstrap.sh"
        ) from exc

    sandboxes = {
        "read-only": Sandbox.read_only,
        "workspace-write": Sandbox.workspace_write,
        "full-access": Sandbox.full_access,
    }
    approval_modes = {"deny-all": ApprovalMode.deny_all}
    config = CodexConfig(
        config_overrides=request.config_overrides,
        cwd=str(request.cwd),
        env=dict(request.environment),
    )
    async with AsyncCodex(config) as codex:
        if request.resume_thread_id is not None:
            if (
                not request.resume_thread_id
                or len(request.resume_thread_id) > 256
                or "\x00" in request.resume_thread_id
            ):
                raise CodexSDKError("Codex resume thread ID is invalid")
            thread = await codex.thread_resume(
                request.resume_thread_id,
                approval_mode=approval_modes[request.approval_mode],
                cwd=str(request.cwd),
                model=request.model,
                sandbox=sandboxes[request.sandbox],
            )
        else:
            thread = await codex.thread_start(
                approval_mode=approval_modes[request.approval_mode],
                cwd=str(request.cwd),
                ephemeral=request.ephemeral,
                model=request.model,
                sandbox=sandboxes[request.sandbox],
            )
        if request.thread_id_path is not None:
            try:
                state_path = request.thread_id_path.resolve(strict=False)
                state_path.parent.resolve(strict=False).relative_to(request.cwd.resolve())
            except (OSError, ValueError) as exc:
                raise CodexSDKError("Codex thread state path is outside its workspace") from exc
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(state_path.parent, 0o700)
            state: dict[str, Any] = {
                "schema_version": 2,
                "thread_id": thread.id,
            }
            if request.thread_state_context is not None:
                state["context"] = dict(request.thread_state_context)
            atomic_write_json(state_path, state)
        turn_input: str | list[Any]
        if request.images:
            turn_input = [TextInput(request.prompt)]
            turn_input.extend(LocalImageInput(str(path)) for path in request.images)
        else:
            turn_input = request.prompt
        result = await thread.run(
            turn_input,
            output_schema=dict(request.output_schema),
        )
    if result.final_response is None:
        raise CodexSDKError("Codex SDK returned no final response")
    return result.final_response


def run_sdk_request(request: CodexSDKRequest) -> str:
    """Run one bounded SDK turn and close its pinned local runtime."""

    try:
        with _isolated_environment(request.environment):
            return asyncio.run(
                asyncio.wait_for(_invoke_sdk(request), request.timeout_seconds)
            )
    except CodexSDKError:
        raise
    except TimeoutError as exc:
        raise CodexSDKError(
            f"Codex SDK turn timed out after {request.timeout_seconds} seconds"
        ) from exc
    except Exception as exc:
        detail = str(exc).strip()[-4000:]
        suffix = f": {detail}" if detail else ""
        raise CodexSDKError(f"Codex SDK invocation failed{suffix}") from exc


def parse_structured_response(
    response: str, *, max_output_bytes: int
) -> dict[str, Any]:
    if not isinstance(response, str):
        raise CodexSDKError("Codex SDK final response is not text")
    encoded = response.encode("utf-8")
    if len(encoded) > max_output_bytes:
        raise CodexSDKError("Codex output exceeds the adapter limit")
    try:
        value = json.loads(response)
    except json.JSONDecodeError as exc:
        raise CodexSDKError("Codex did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise CodexSDKError("Codex output is not a JSON object")
    return value


def run_structured_request(
    request: CodexSDKRequest,
    *,
    max_output_bytes: int,
    sdk_runner: CodexSDKRunner | None = None,
) -> dict[str, Any]:
    runner = run_sdk_request if sdk_runner is None else sdk_runner
    try:
        response = runner(request)
    except CodexSDKError:
        raise
    except Exception as exc:
        detail = str(exc).strip()[-4000:]
        suffix = f": {detail}" if detail else ""
        raise CodexSDKError(f"Codex SDK invocation failed{suffix}") from exc
    return parse_structured_response(response, max_output_bytes=max_output_bytes)


def run_structured_codex(
    prompt: bytes,
    output_schema: Mapping[str, Any],
    *,
    timeout_variable: str,
    model_variable: str,
    default_timeout_seconds: int,
    workspace_prefix: str,
    environ: Mapping[str, str] | None = None,
    sdk_runner: CodexSDKRunner | None = None,
) -> dict[str, Any]:
    if len(prompt) > MAX_PROMPT_BYTES:
        raise CodexSDKError("Codex prompt exceeds the adapter input limit")
    try:
        prompt_text = prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexSDKError("Codex prompt is not UTF-8") from exc

    source_env = os.environ if environ is None else environ
    environment = diagnostic_environment(source_env)
    timeout = _timeout_seconds(
        source_env, variable=timeout_variable, default=default_timeout_seconds
    )
    model = _model(source_env, variable=model_variable)
    config_overrides = feature_overrides(READ_ONLY_DISABLED_FEATURES) + (
        "mcp_servers={}",
        "skills.config=[]",
        "tools.web_search=false",
        'shell_environment_policy.inherit="none"',
    )

    with tempfile.TemporaryDirectory(prefix=workspace_prefix) as raw_workspace:
        workspace = Path(raw_workspace)
        workspace.chmod(0o500)
        try:
            request = CodexSDKRequest(
                prompt=prompt_text,
                output_schema=dict(output_schema),
                cwd=workspace,
                sandbox="read-only",
                images=(),
                model=model,
                timeout_seconds=timeout,
                environment=environment,
                config_overrides=config_overrides,
            )
            return run_structured_request(
                request,
                max_output_bytes=MAX_OUTPUT_BYTES,
                sdk_runner=sdk_runner,
            )
        finally:
            # TemporaryDirectory needs write permission for cleanup.
            workspace.chmod(0o700)


def doctor() -> None:
    try:
        version = importlib.metadata.version("openai-codex")
        from openai_codex import Codex, CodexConfig
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise CodexSDKError(
            "the openai-codex SDK is unavailable; run scripts/bootstrap.sh"
        ) from exc

    environment = diagnostic_environment(os.environ)
    config = CodexConfig(
        config_overrides=feature_overrides(READ_ONLY_DISABLED_FEATURES)
        + ("mcp_servers={}", "skills.config=[]", "tools.web_search=false"),
        env=environment,
    )
    try:
        with _isolated_environment(environment):
            with Codex(config) as codex:
                account = codex.account()
    except Exception as exc:
        detail = str(exc).strip()[-2000:]
        suffix = f": {detail}" if detail else ""
        raise CodexSDKError(f"Codex SDK health check failed{suffix}") from exc
    if getattr(account, "account", None) is None:
        raise CodexSDKError("Codex SDK has no authenticated account")
    print(f"openai-codex SDK {version}; authentication available")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments != ["doctor"]:
        print("usage: python -m maa_planner.codex_sdk doctor", file=sys.stderr)
        return 2
    try:
        doctor()
    except CodexSDKError as exc:
        print(f"maa-codex-sdk: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
