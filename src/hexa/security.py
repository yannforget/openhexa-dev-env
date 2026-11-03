"""Diagnostic redaction, separate from explicitly requested payload delivery."""

import json
import re
from typing import IO, Any, Protocol


class Writer(Protocol):
    """A text destination for redacted output."""

    def write(self, text: str, /) -> object: ...

    def flush(self) -> None: ...


class Redactor:
    """Filter known tokens and HTTP(S) URLs, including final partial lines."""

    def __init__(self, token: str, secrets: tuple[str, ...] = ()):
        self.token = token
        expanded = list(secrets)
        for secret in secrets:
            if secret.lstrip().startswith("{"):
                try:
                    parsed = json.loads(secret)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    expanded.extend(
                        value for value in parsed.values() if isinstance(value, str)
                    )
        self.secrets = sorted(
            {
                variant
                for secret in expanded
                if secret
                for variant in (secret, json.dumps(secret)[1:-1], *secret.splitlines())
                if variant
            },
            key=len,
            reverse=True,
        )

    def text(self, text: str) -> str:
        """Return safe display text, not a copy for raw log retention."""
        text = text.replace(self.token, "[TOKEN withheld]")
        for secret in self.secrets:
            text = text.replace(secret, "[CONNECTION withheld]")
        text = re.sub(
            r"/[A-Za-z0-9_-]{43}(?=/|[\s'\"<>]|$)(?:/[^\s'\"<>]*)?",
            "[LOCAL URL withheld]",
            text,
        )
        return re.sub(
            r"https?://[^\s\"<>]+", "[URL withheld]", text, flags=re.IGNORECASE
        )

    def value(self, value: Any) -> Any:
        """Redact nested public records without corrupting their JSON structure."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    def stream(self, source: IO[str], destination: Writer) -> None:
        """Buffer lines across writes; withhold oversized lines entirely."""
        while line := source.readline(65537):
            if len(line) > 65536:
                while not line.endswith("\n"):
                    line = source.readline(65537)
                    if not line:
                        break
                destination.write("[oversized output line withheld]\n")
            else:
                destination.write(self.text(line))
            destination.flush()
