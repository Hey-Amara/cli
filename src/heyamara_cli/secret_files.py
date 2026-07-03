from __future__ import annotations

import errno
import os
from os import PathLike
from pathlib import Path
from secrets import token_hex
from typing import Optional, Union

SECRET_DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600
SecretPath = Union[str, PathLike[str]]


class UnsafeSecretFileError(OSError):
    """Raised when a secret output path is unsafe to write."""


def _unsafe_symlink_message(path: Path) -> str:
    return (
        f"Refusing to write secret file through symlink: {path}. "
        "Remove the symlink or choose a regular output file."
    )


def _raise_unsafe_symlink(path: Path) -> None:
    raise UnsafeSecretFileError(_unsafe_symlink_message(path))


def _is_trusted_platform_symlink(component: Path) -> bool:
    """Allow root-owned top-level platform aliases such as macOS /var."""
    if os.name == "nt" or not component.is_absolute() or len(component.parts) != 2:
        return False
    try:
        return component.lstat().st_uid == 0 and component.resolve() != component
    except OSError:
        return False


def _existing_path_parts(path: Path) -> list[Path]:
    """Return existing path components from broadest to narrowest."""
    if not path.parts:
        return []

    if path.is_absolute():
        current = Path(path.anchor)
        parts = list(path.parts[1:])
    else:
        current = Path(path.parts[0])
        parts = list(path.parts[1:])

    existing: list[Path] = []
    if current.exists() or current.is_symlink():
        existing.append(current)
    else:
        return existing

    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            existing.append(current)
        else:
            break
    return existing


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks in an existing path prefix before writing secrets."""
    for component in _existing_path_parts(path):
        try:
            if component.is_symlink() and not _is_trusted_platform_symlink(component):
                _raise_unsafe_symlink(component)
        except UnsafeSecretFileError:
            raise
        except OSError as exc:
            raise UnsafeSecretFileError(f"Unable to inspect secret path: {component}") from exc


def _private_chmod_fd(path: Path, mode: int, *, directory: bool) -> bool:
    """Apply chmod via an fd where POSIX supports it."""
    if os.name == "nt" or os.open not in getattr(os, "supports_dir_fd", set()):
        return False

    flags = os.O_RDONLY
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _raise_unsafe_symlink(path)
        raise

    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    return True


def _chmod_path_private(path: Path, mode: int, *, directory: bool) -> None:
    """Set private permissions without following symlink leafs where possible."""
    if os.name == "nt":
        return
    if not _private_chmod_fd(path, mode, directory=directory):
        os.chmod(path, mode)


def _missing_dirs(directory: Path) -> list[Path]:
    missing: list[Path] = []
    current = directory
    while not (current.exists() or current.is_symlink()):
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return missing


def _ensure_private_created_dirs(directory: Path) -> None:
    missing = _missing_dirs(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    for created in reversed(missing):
        if created.is_symlink():
            _raise_unsafe_symlink(created)
        if created.is_dir():
            _chmod_path_private(created, SECRET_DIR_MODE, directory=True)


def ensure_private_dir(path: SecretPath) -> None:
    """Create a directory for local secrets with private POSIX permissions."""
    directory = Path(path)
    _reject_symlink_components(directory)
    _ensure_private_created_dirs(directory)

    if directory.is_symlink():
        _raise_unsafe_symlink(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Secret output path is not a directory: {directory}")
    _chmod_path_private(directory, SECRET_DIR_MODE, directory=True)


def _open_parent_dir(parent: Path) -> Optional[int]:
    if os.name == "nt" or os.open not in getattr(os, "supports_dir_fd", set()):
        return None

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        return os.open(parent, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _raise_unsafe_symlink(parent)
        raise


def _open_temp_file(parent: Path, parent_fd: Optional[int], final_name: str) -> tuple[int, str, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    last_exc: Optional[OSError] = None
    for _ in range(20):
        temp_name = f".{final_name}.{os.getpid()}.{token_hex(8)}.tmp"
        temp_path = parent / temp_name
        try:
            if parent_fd is not None:
                fd = os.open(temp_name, flags, SECRET_FILE_MODE, dir_fd=parent_fd)
            else:
                fd = os.open(temp_path, flags, SECRET_FILE_MODE)
            return fd, temp_name, temp_path
        except FileExistsError as exc:
            last_exc = exc
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _raise_unsafe_symlink(temp_path)
            raise
    raise FileExistsError(f"Unable to create a temporary secret file near {parent}") from last_exc


def _replace_from_temp(
    parent_fd: Optional[int],
    temp_name: str,
    temp_path: Path,
    final_name: str,
    final_path: Path,
) -> None:
    if parent_fd is not None and os.rename in getattr(os, "supports_dir_fd", set()):
        os.rename(temp_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    else:
        os.replace(temp_path, final_path)


def _cleanup_temp(parent_fd: Optional[int], temp_name: Optional[str], temp_path: Optional[Path]) -> None:
    if not temp_name or temp_path is None:
        return
    try:
        if parent_fd is not None and os.unlink in getattr(os, "supports_dir_fd", set()):
            os.unlink(temp_name, dir_fd=parent_fd)
        else:
            temp_path.unlink()
    except FileNotFoundError:
        pass


def write_secret_text(path: SecretPath, content: str, *, trailing_newline: bool = False) -> None:
    """Atomically write secret-bearing text with private file permissions."""
    output_path = Path(path)
    if output_path.name in {"", ".", ".."}:
        raise IsADirectoryError(f"Secret output path is not a regular file path: {output_path}")

    _reject_symlink_components(output_path)

    parent = output_path.parent
    _ensure_private_created_dirs(parent)
    if parent.is_symlink():
        _raise_unsafe_symlink(parent)
    if not parent.is_dir():
        raise NotADirectoryError(f"Secret output parent is not a directory: {parent}")

    parent_fd = _open_parent_dir(parent)
    fd = None
    temp_name: Optional[str] = None
    temp_path: Optional[Path] = None
    replaced = False
    try:
        fd, temp_name, temp_path = _open_temp_file(parent, parent_fd, output_path.name)
        if os.name != "nt" and hasattr(os, "fchmod"):
            os.fchmod(fd, SECRET_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            fd = None
            f.write(content)
            if trailing_newline and not content.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        _replace_from_temp(parent_fd, temp_name, temp_path, output_path.name, output_path)
        replaced = True
    finally:
        if fd is not None:
            os.close(fd)
        if not replaced:
            _cleanup_temp(parent_fd, temp_name, temp_path)
        if parent_fd is not None:
            os.close(parent_fd)
