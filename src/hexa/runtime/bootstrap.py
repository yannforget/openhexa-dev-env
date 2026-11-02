"""Inject the private stdin token before executing ordinary mounted source."""

import importlib
import json
import os
import runpy
import sys
from pathlib import Path

sitecustomize = importlib.import_module("sitecustomize")

# Exit status reported to the host when a mount cannot be read.
UNREADABLE_MOUNT = 77


def _unreadable(entrypoint: Path) -> tuple[Path, str] | None:
    """Return the first unreadable mount path and the reason, if any."""
    inputs = Path("/inputs")
    directories = [entrypoint.parent]
    if inputs.is_dir():
        directories.extend(sorted(inputs.iterdir()))
    for path in (*directories, entrypoint):
        try:
            if path.is_dir():
                os.listdir(path)
            else:
                path.open("rb").close()
        except PermissionError:
            return path, "permission denied"
        except OSError as error:
            return path, (error.strerror or type(error).__name__).lower()
    return None


def main() -> None:
    """Validate startup and hand control to the pipeline's normal main guard."""
    if not sitecustomize.adapter_ready:
        raise RuntimeError("Connected-local startup adapter did not initialize")
    entrypoint = sys.argv[1]
    unreadable = _unreadable(Path(entrypoint))
    if unreadable is not None:
        print(f"hexa: cannot read {unreadable[0]} ({unreadable[1]})", file=sys.stderr)
        sys.exit(UNREADABLE_MOUNT)
    payload = json.loads(sys.stdin.readline())
    if set(payload) != {"token", "parameters"} or not isinstance(payload["token"], str):
        raise RuntimeError("Invalid private bootstrap payload")
    os.environ["HEXA_TOKEN"] = payload.pop("token")
    sys.argv = [entrypoint, "-c", json.dumps(payload["parameters"])]
    del payload
    runpy.run_path(entrypoint, run_name="__main__")


if __name__ == "__main__":
    main()
