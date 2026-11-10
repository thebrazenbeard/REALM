"""Optional loopback model proposal transport for host-bound REALM residents.

No download, inference runtime, scheduler, agent memory, cloud access, or
autonomous authority is created here. The host provides a private view and
fixed identity; a local model returns only an untrusted command proposal.
"""

from __future__ import annotations

import http.client
import json
import math
import time
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import urlsplit

from .residents import ResidentController, ResidentProposal


_MAX_VIEW_CHARS = 6000
_MAX_RESPONSE_BYTES = 8192
_SYSTEM_CONTRACT = (
    'Propose exactly one JSON object with the single key "command". '
    'Do not include markdown, actor, world, action receipt, or unsupported '
    'claims of completed gameplay. The host decides all authority and effects.'
)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field in local model response")
        result[key] = value
    return result


@dataclass(frozen=True)
class LocalChatConfig:
    """One host-approved local endpoint using an OpenAI-style chat schema."""

    endpoint: str
    model: str
    timeout_seconds: float = 5.0
    max_tokens: int = 96

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str):
            raise ValueError("Host must supply a local HTTP endpoint")
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Malformed local model endpoint") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment
            or any(ord(char) < 32 or ord(char) == 127 for char in self.endpoint)
        ):
            raise ValueError("Model endpoint must be a credential-free literal loopback HTTP URL")
        if (
            not isinstance(self.model, str) or not self.model
            or len(self.model) > 128 or not all(char.isprintable() for char in self.model)
        ):
            raise ValueError("Host must choose a bounded printable local model identifier")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 60
        ):
            raise ValueError("Local model timeout must be finite in (0,60] seconds")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 256:
            raise ValueError("Local generation budget must be 1–256 tokens")


class LocalChatProposer:
    """One untrusted local proposal; host bound views/actors remain external."""

    def __init__(self, config: LocalChatConfig) -> None:
        if not isinstance(config, LocalChatConfig):
            raise TypeError("LocalChatProposer requires a host-approved config")
        self.config = config

    def propose(self, host_view: str) -> dict[str, str]:
        if (
            not isinstance(host_view, str)
            or not 1 <= len(host_view) <= _MAX_VIEW_CHARS
            or not all(char.isprintable() or char in "\n\r\t" for char in host_view)
        ):
            raise ValueError("Host view must be 1–6000 printable characters")
        endpoint = urlsplit(self.config.endpoint)
        request_bytes = json.dumps({
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_CONTRACT},
                {"role": "user", "content": host_view},
            ],
            "temperature": 0,
            "max_tokens": self.config.max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        # Direct HTTPConnection does not honor environment proxy settings,
        # resolve remote hostnames, or follow remote redirects.
        with closing(http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=self.config.timeout_seconds,
        )) as connection:
            deadline = time.monotonic() + self.config.timeout_seconds
            connection.request(
                "POST", endpoint.path, body=request_bytes,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"Local model HTTP status {response.status}; no action committed")
            # HTTPResponse.read(n) may wait for all n bytes while a local
            # inference server drips data indefinitely. Incremental reads
            # let the host enforce a wall-clock body budget across chunks,
            # independently of the socket's idle-read timeout.
            chunks: list[bytes] = []
            length = 0
            while length <= _MAX_RESPONSE_BYTES:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Local model response deadline exceeded")
                chunk = response.read1(min(2048, _MAX_RESPONSE_BYTES + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            body = b"".join(chunks)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("Local model response exceeds configured byte limit")
        try:
            envelope = json.loads(body, object_pairs_hook=_unique_json_object)
            if not isinstance(envelope, dict):
                raise ValueError("Local model response must be a JSON object")
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Local model response must contain one completion choice")
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("Local model response has no chat message")
            content = choice["message"].get("content")
            if not isinstance(content, str):
                raise ValueError("Local model response has no text content")
            command = json.loads(content, object_pairs_hook=_unique_json_object)
            parsed = ResidentProposal.from_untrusted(command)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Local model did not emit strict JSON command") from exc
        return {"command": parsed.command}

    def commit(self, controller: ResidentController, host_view: str, *, action_id: str) -> str:
        """PROPOSE then host VALIDATE and Engine COMMIT; not autonomous."""
        if not isinstance(controller, ResidentController):
            raise TypeError("Host must supply a resident controller")
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
            raise ValueError("Host action ID must be nonempty text up to 128 characters")
        return controller.commit(self.propose(host_view), action_id=action_id)
