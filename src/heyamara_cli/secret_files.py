from __future__ import annotations

import errno
import os
from pathlib import Path

SECRET_DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600


class UnsafeSecretFileError(OSError):
    """Raised when a secret output path is unsafe to write."""


def ensure_private_dir(path: str | Path) -> None:
    """Create a directory for local secrets with private POSIX permissions."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    if os.name != "nt":
        os.chmod(directory, SECRET_DIR_MODE)


def write_secret_text(path: str | Path, content: str, *, trailing_newline: bool = False) -> None:
    """Write secret-bearing text with private file permissions where supported."""
    output_path = Path(path)
    if output_path.is_symlink():
        raise UnsafeSecretFileError(f"Refusing to write secret file through symlink: {output_path}")

    parent = output_path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(output_path, flags, SECRET_FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeSecretFileError(
                f"Refusing to write secret file through symlink: {output_path}"
            ) from exc
        raise

    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            if trailing_newline and not content.endswith("\n"):
                f.write("\n")
    finally:
        if os.name != "nt" and output_path.exists():
            os.chmod(output_path, SECRET_FILE_MODE)
