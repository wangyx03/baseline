"""Read / update the project-root .env shared by every TikTok Shop service.

One implementation for both the OAuth callback (initial tokens) and the API
client (refreshed tokens). Writes are atomic: temp file + os.replace, so a
crash mid-write never leaves a truncated .env behind.
"""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


def read_env_file() -> dict:
    """Parse .env without python-dotenv. Supports KEY=value / "value" / 'value'."""
    if not ENV_FILE.exists():
        raise RuntimeError(f".env file not found: {ENV_FILE}")

    values = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def update_env_values(updates: dict):
    """Update only the given keys; keep comments, blank lines and other keys.

    Keys not yet present are appended at the end.
    """
    if not ENV_FILE.exists():
        raise RuntimeError(f".env file not found: {ENV_FILE}")

    remaining = dict(updates)
    new_lines = []

    for original_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = original_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(original_line)

    if remaining:
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        for key, value in remaining.items():
            new_lines.append(f"{key}={value}")

    new_text = "\n".join(new_lines) + "\n"

    fd, temp_path = tempfile.mkstemp(prefix=".env.tmp.", dir=str(ENV_FILE.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(temp_path, ENV_FILE)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
