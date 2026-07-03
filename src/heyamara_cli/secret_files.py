from __future__ import annotations

import errno
import os
import stat
from os import PathLike
from pathlib import Path
from secrets import token_hex
from typing import Optional, Union

SECRET_DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600
SecretPath = Union[str, PathLike[str]]

_SUPPORTS_DIR_FD = os.name != "nt" and os.open in getattr(os, "supports_dir_fd", set())
_SUPPORTS_MKDIR_DIR_FD = os.name != "nt" and os.mkdir in getattr(os, "supports_dir_fd", set())
_SUPPORTS_RENAME_DIR_FD = os.name != "nt" and os.rename in getattr(os, "supports_dir_fd", set())
_SUPPORTS_UNLINK_DIR_FD = os.name != "nt" and os.unlink in getattr(os, "supports_dir_fd", set())
_SUPPORTS_STAT_DIR_FD = os.name != "nt" and os.stat in getattr(os, "supports_dir_fd", set())


class UnsafeSecretFileError(OSError):
    """Raised when a secret output path is unsafe to write."""


def _unsafe_symlink_message(path: Path) -> str:
    return (
        f"Refusing to write secret file through symlink: {path}. "
        "Remove the symlink or choose a regular output file."
    )


def _raise_unsafe_symlink(path: Path) -> None:
    raise UnsafeSecretFileError(_unsafe_symlink_message(path))


def _raise_write_error(action: str, path: Path, exc: OSError) -> None:
    detail = exc.strerror or str(exc)
    raise UnsafeSecretFileError(
        f"Could not {action} secret output path {path}: {detail}. "
        "Choose a regular path and check write permissions."
    ) from exc


def _is_trusted_platform_symlink(component: Path) -> bool:
    """Allow root-owned top-level platform aliases such as macOS /var."""
    if os.name == "nt" or not component.is_absolute() or len(component.parts) != 2:
        return False
    try:
        return component.lstat().st_uid == 0 and component.resolve() != component
    except OSError:
        return False


def _is_windows_reparse_point(path: Path) -> bool:
    """Return True for Windows junction/reparse points that can redirect writes."""
    if os.name != "nt":
        return False

    is_junction = getattr(path, "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _is_unsafe_link_component(path: Path) -> bool:
    if path.is_symlink():
        return not _is_trusted_platform_symlink(path)
    return _is_windows_reparse_point(path)


def _normalize_trusted_platform_alias(path: Path) -> Path:
    """Resolve root-owned platform aliases in absolute paths before fd walking."""
    if not path.is_absolute() or len(path.parts) < 2:
        return path

    alias = Path(path.anchor) / path.parts[1]
    if _is_trusted_platform_symlink(alias):
        return alias.resolve().joinpath(*path.parts[2:])
    return path


def _existing_path_parts(path: Path) -> list[Path]:
    """Return existing path components from broadest to narrowest."""
    path = _normalize_trusted_platform_alias(path)
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
    """Best-effort fallback symlink check for platforms without dir-fd walking."""
    for component in _existing_path_parts(path):
        try:
            if _is_unsafe_link_component(component):
                _raise_unsafe_symlink(component)
        except UnsafeSecretFileError:
            raise
        except OSError as exc:
            _raise_write_error("inspect", component, exc)


def _dir_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _can_walk_with_dir_fd() -> bool:
    return _SUPPORTS_DIR_FD and _SUPPORTS_MKDIR_DIR_FD


def _open_start_dir(path: Path) -> int:
    start = path.anchor if path.is_absolute() else "."
    return os.open(start, _dir_flags())


def _path_parts_for_walk(path: Path) -> list[str]:
    path = _normalize_trusted_platform_alias(path)
    parts = list(path.parts[1:] if path.is_absolute() else path.parts)
    return [part for part in parts if part not in {"", "."}]


def _child_is_symlink(parent_fd: int, part: str) -> bool:
    if not _SUPPORTS_STAT_DIR_FD:
        return False
    try:
        return stat.S_ISLNK(os.stat(part, dir_fd=parent_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open_dir_child(parent_fd: int, part: str, display_path: Path) -> int:
    try:
        return os.open(part, _dir_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP or (
            exc.errno == errno.ENOTDIR and _child_is_symlink(parent_fd, part)
        ):
            _raise_unsafe_symlink(display_path)
        if exc.errno in {errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            _raise_write_error("open", display_path, exc)
        raise


def _open_directory_fd(path: Path, *, create_missing: bool) -> tuple[int, bool, Path]:
    """Open a directory by walking each component with O_NOFOLLOW dir-fds."""
    path = _normalize_trusted_platform_alias(path)
    fd = _open_start_dir(path)
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    created_final = False

    try:
        parts = _path_parts_for_walk(path)
        for index, part in enumerate(parts):
            current = current / part
            try:
                child_fd = _open_dir_child(fd, part, current)
            except FileNotFoundError as exc:
                if not create_missing:
                    _raise_write_error("open", current, exc)
                created_by_this_call = True
                try:
                    os.mkdir(part, SECRET_DIR_MODE, dir_fd=fd)
                except OSError as mkdir_exc:
                    if (
                        not isinstance(mkdir_exc, FileExistsError)
                        and mkdir_exc.errno != errno.EEXIST
                    ):
                        _raise_write_error("create", current, mkdir_exc)
                    created_by_this_call = False
                child_fd = _open_dir_child(fd, part, current)
                if hasattr(os, "fchmod") and created_by_this_call:
                    try:
                        os.fchmod(child_fd, SECRET_DIR_MODE)
                    except Exception:
                        os.close(child_fd)
                        raise
                if index == len(parts) - 1 and created_by_this_call:
                    created_final = True
            os.close(fd)
            fd = child_fd
        return fd, created_final, path
    except Exception:
        os.close(fd)
        raise


def _private_chmod_fd(path: Path, mode: int, *, directory: bool) -> bool:
    """Apply chmod via an fd where POSIX supports it."""
    if os.name == "nt" or not _SUPPORTS_DIR_FD:
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


def _ensure_private_created_dirs(directory: Path) -> bool:
    missing = _missing_dirs(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    for created in reversed(missing):
        if _is_unsafe_link_component(created):
            _raise_unsafe_symlink(created)
        if created.is_dir():
            _chmod_path_private(created, SECRET_DIR_MODE, directory=True)
    return bool(missing)


def ensure_private_dir(path: SecretPath, *, chmod_existing: bool = True) -> None:
    """Create a directory for local secrets with private POSIX permissions.

    When ``chmod_existing`` is false, only directories created by this call are
    chmodded. That lets commands accept caller-owned existing output directories
    without silently changing their permissions.
    """
    directory = Path(path)
    try:
        if _can_walk_with_dir_fd():
            fd, created, _ = _open_directory_fd(directory, create_missing=True)
            try:
                if chmod_existing or created:
                    os.fchmod(fd, SECRET_DIR_MODE)
            finally:
                os.close(fd)
            return

        _reject_symlink_components(directory)
        existed = directory.exists() or _is_unsafe_link_component(directory)
        created = _ensure_private_created_dirs(directory)
        if _is_unsafe_link_component(directory):
            _raise_unsafe_symlink(directory)
        if not directory.is_dir():
            raise NotADirectoryError(f"Secret output path is not a directory: {directory}")
        if chmod_existing or created or not existed:
            _chmod_path_private(directory, SECRET_DIR_MODE, directory=True)
    except UnsafeSecretFileError:
        raise
    except OSError as exc:
        _raise_write_error("prepare", directory, exc)


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


def _ensure_final_path_is_regular_or_missing(parent_fd: Optional[int], path: Path, final_name: str) -> None:
    try:
        if parent_fd is not None and _SUPPORTS_STAT_DIR_FD:
            mode = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False).st_mode
        else:
            if _is_unsafe_link_component(path):
                _raise_unsafe_symlink(path)
            mode = path.lstat().st_mode
    except FileNotFoundError:
        return

    if stat.S_ISLNK(mode):
        _raise_unsafe_symlink(path)
    if stat.S_ISDIR(mode):
        raise IsADirectoryError(f"Secret output path is a directory: {path}")
    if not stat.S_ISREG(mode):
        raise UnsafeSecretFileError(
            f"Secret output path is not a regular file: {path}. "
            "Choose a regular output file."
        )


def _replace_from_temp(
    parent_fd: Optional[int],
    temp_name: str,
    temp_path: Path,
    final_name: str,
    final_path: Path,
) -> None:
    if parent_fd is not None and _SUPPORTS_RENAME_DIR_FD:
        os.rename(temp_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    else:
        os.replace(temp_path, final_path)


def _sync_parent_dir(parent_fd: Optional[int], path: Path) -> None:
    if parent_fd is None:
        return
    try:
        os.fsync(parent_fd)
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise UnsafeSecretFileError(
            f"Secret output was written to {path}, but final directory sync failed: {detail}. "
            "Verify the file before retrying."
        ) from exc


def _cleanup_temp(parent_fd: Optional[int], temp_name: Optional[str], temp_path: Optional[Path]) -> None:
    if not temp_name or temp_path is None:
        return
    try:
        if parent_fd is not None and _SUPPORTS_UNLINK_DIR_FD:
            os.unlink(temp_name, dir_fd=parent_fd)
        else:
            temp_path.unlink()
    except OSError:
        pass


def write_secret_text(path: SecretPath, content: str, *, trailing_newline: bool = False) -> None:
    """Atomically write secret-bearing text with private file permissions."""
    output_path = Path(path)
    if output_path.name in {"", ".", ".."}:
        raise UnsafeSecretFileError(
            f"Secret output path is not a regular file path: {output_path}. "
            "Choose a regular output file."
        )

    parent = output_path.parent
    parent_fd: Optional[int] = None
    fd: Optional[int] = None
    temp_name: Optional[str] = None
    temp_path: Optional[Path] = None
    replaced = False

    try:
        if _can_walk_with_dir_fd():
            parent_fd, _, normalized_parent = _open_directory_fd(parent, create_missing=False)
            output_path = normalized_parent / output_path.name
        else:
            output_path = _normalize_trusted_platform_alias(output_path)
            parent = output_path.parent
            _reject_symlink_components(output_path)
            if _is_unsafe_link_component(parent):
                _raise_unsafe_symlink(parent)
            if not parent.is_dir():
                raise NotADirectoryError(f"Secret output parent is not a directory: {parent}")

        _ensure_final_path_is_regular_or_missing(parent_fd, output_path, output_path.name)
        fd, temp_name, temp_path = _open_temp_file(output_path.parent, parent_fd, output_path.name)
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
        _sync_parent_dir(parent_fd, output_path)
    except UnsafeSecretFileError:
        raise
    except OSError as exc:
        _raise_write_error("write", Path(path), exc)
    finally:
        if fd is not None:
            os.close(fd)
        if not replaced:
            _cleanup_temp(parent_fd, temp_name, temp_path)
        if parent_fd is not None:
            os.close(parent_fd)
