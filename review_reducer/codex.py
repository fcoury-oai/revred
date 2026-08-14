"""Fresh Codex CLI sessions with strict, dependency-free output validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
import json
from pathlib import Path
import subprocess
import threading
from typing import Any

from review_reducer.errors import CodexInvocationError, InvalidReviewError


def schema_path(name: str) -> Path:
    return Path(str(resources.files("review_reducer").joinpath("schemas", name)))


def validate_schema(value: Any, schema: dict[str, Any], location: str = "$") -> None:
    """Validate the small schema subset used by our checked-in contracts."""

    expected = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if expected and not valid.get(expected, False):
        raise InvalidReviewError(f"structured output {location} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidReviewError(f"structured output {location} has an unsupported value")
    if expected == "object":
        required = set(schema.get("required", []))
        missing = sorted(required.difference(value))
        if missing:
            raise InvalidReviewError(
                f"structured output {location} is missing {', '.join(missing)}"
            )
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value).difference(properties))
            if extras:
                raise InvalidReviewError(
                    f"structured output {location} has unexpected {', '.join(extras)}"
                )
        for key, child in properties.items():
            if key in value:
                validate_schema(value[key], child, f"{location}.{key}")
    elif expected == "array":
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, f"{location}[{index}]")


@dataclass(slots=True)
class CodexRunner:
    repo: Path
    run_dir: Path
    binary: str = "codex"
    review_model: str | None = None
    verifier_model: str | None = None
    fixer_model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = 1200
    _command_lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._command_lock = threading.Lock()

    def _settings(self, model: str | None) -> list[str]:
        settings: list[str] = []
        if model:
            settings.extend(["--model", model])
        if self.reasoning_effort:
            settings.extend(
                ["-c", f"model_reasoning_effort={json.dumps(self.reasoning_effort)}"]
            )
        return settings

    def _invoke(self, label: str, command: list[str], prompt: str | None) -> str:
        response_path = self.run_dir / f"{label}.response.txt"
        events_path = self.run_dir / f"{label}.events.jsonl"
        if prompt is not None:
            (self.run_dir / f"{label}.prompt.txt").write_text(prompt, encoding="utf-8")
        command.extend(["--output-last-message", str(response_path)])
        with self._command_lock:
            with (self.run_dir / "commands.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"role": label, "argv": command}) + "\n")
        try:
            with events_path.open("w", encoding="utf-8") as events:
                result = subprocess.run(
                    command,
                    cwd=self.repo,
                    text=True,
                    input=prompt,
                    stdout=events,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.timeout_seconds,
                )
        except FileNotFoundError as error:
            raise CodexInvocationError(f"Codex executable not found: {self.binary}") from error
        except subprocess.TimeoutExpired as error:
            raise CodexInvocationError(
                f"Codex {label} exceeded the {self.timeout_seconds}s timeout"
            ) from error
        stderr_path = self.run_dir / f"{label}.stderr.txt"
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
        if result.returncode:
            detail = (result.stderr or "").strip().splitlines()
            message = detail[-1] if detail else f"exit status {result.returncode}"
            raise CodexInvocationError(f"Codex {label} failed: {message}")
        if not response_path.is_file():
            raise InvalidReviewError(f"Codex {label} did not produce a final response")
        response = response_path.read_text(encoding="utf-8").strip()
        if not response:
            raise InvalidReviewError(f"Codex {label} produced an empty final response")
        return response

    def native_review(self, base_ref: str, label: str) -> str:
        command = [
            self.binary,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--json",
        ]
        if self.review_model:
            command.extend(["-c", f"review_model={json.dumps(self.review_model)}"])
        if self.reasoning_effort:
            command.extend(
                ["-c", f"model_reasoning_effort={json.dumps(self.reasoning_effort)}"]
            )
        command.extend(["review", "--base", base_ref])
        return self._invoke(label, command, None)

    def structured_turn(
        self,
        *,
        label: str,
        prompt: str,
        schema_name: str,
        writable: bool = False,
    ) -> dict[str, Any]:
        schema_file = schema_path(schema_name)
        role_model = self.fixer_model if writable else self.verifier_model
        command = [
            self.binary,
            "exec",
            "--sandbox",
            "workspace-write" if writable else "read-only",
            "--ephemeral",
            "--json",
            "--disable",
            "multi_agent",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "memories",
            "-c",
            "skills.include_instructions=false",
            "--output-schema",
            str(schema_file),
            *self._settings(role_model),
            "-",
        ]
        response = self._invoke(label, command, prompt)
        try:
            value = json.loads(response)
        except json.JSONDecodeError as error:
            raise InvalidReviewError(f"Codex {label} did not return valid JSON") from error
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        validate_schema(value, schema)
        assert isinstance(value, dict)
        return value

    def usage_summary(self) -> dict[str, Any]:
        """Read actual Codex turn telemetry without estimating token counts."""

        keys = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        totals = {key: 0 for key in keys}
        turns: list[dict[str, Any]] = []
        suffix = ".events.jsonl"
        for path in sorted(self.run_dir.glob(f"*{suffix}")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "turn.completed":
                    continue
                usage = event.get("usage") or {}
                turn = {"role": path.name[: -len(suffix)]}
                for key in keys:
                    value = usage.get(key, 0)
                    amount = value if isinstance(value, int) and not isinstance(value, bool) else 0
                    turn[key] = amount
                    totals[key] += amount
                turns.append(turn)
        return {"turn_count": len(turns), **totals, "turns": turns}
