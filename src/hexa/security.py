"""Diagnostic redaction, separate from explicitly requested payload delivery."""

import re
from typing import IO, Protocol


class Writer(Protocol):
    """A text destination for redacted output."""

    def write(self, text: str, /) -> object: ...

    def flush(self) -> None: ...


class Redactor:
    """Filter known tokens and HTTP(S) URLs, including final partial lines."""

    def __init__(self, token: str):
        self.token = token

    def text(self, text: str) -> str:
        """Return safe display text, not a copy for raw log retention."""
        text = text.replace(self.token, "[TOKEN withheld]")
        return re.sub(
            r"https?://[^\s\"<>]+", "[URL withheld]", text, flags=re.IGNORECASE
        )

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
