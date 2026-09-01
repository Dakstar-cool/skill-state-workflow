#!/usr/bin/env python3
"""Deterministic guard for compact, persistent SKILL.state JSON.

The guard owns only the state-file transaction. External actions remain outside
that transaction and must be reconciled separately when their outcome is unsure.
"""

from __future__ import annotations

import argparse
import copy
import errno
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 32 * 1024
DEFAULT_LOCK_TIMEOUT = 5.0
MAX_DEPTH = 32
MAX_COLLECTION_ITEMS = 4096
MAX_INTEGER = 2**53 - 1

EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_IO = 4

REQUIRED_KEYS = {
    "schema_version",
    "task_id",
    "owner",
    "objective",
    "objective_revision",
    "state_revision",
    "updated_at",
    "success_criteria",
    "facts",
    "pending",
}
OPTIONAL_KEYS = {
    "last_observation",
    "phase",
    "decisions",
    "artifacts",
    "risks",
    "uncertainties",
    "retry_counts",
}
PROTECTED_PATCH_KEYS = {
    "schema_version",
    "task_id",
    "owner",
    "objective_revision",
    "state_revision",
    "updated_at",
}
CRITERION_KEYS = {"statement", "status", "evidence_ref"}
EVIDENCE_REQUIRED_KEYS = {"ref", "objective_revision", "scope"}
EVIDENCE_OPTIONAL_KEYS = {"source", "observed_at"}
STATUSES = {"unknown", "passed", "failed"}


class StateGuardError(Exception):
    """Expected validation, conflict, or I/O failure with a stable code."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _fail(code: str, message: str, exit_code: int = EXIT_USAGE) -> NoReturn:
    raise StateGuardError(code, message, exit_code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _absolute(path: os.PathLike[str] | str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if os.name == "nt":
        rendered = str(absolute)
        if rendered.startswith("\\\\"):
            _fail("UNSAFE_PATH", f"Network paths are not supported: {absolute}")
        device_names = {"CON", "PRN", "AUX", "NUL"}
        device_names.update({f"COM{index}" for index in range(1, 10)})
        device_names.update({f"LPT{index}" for index in range(1, 10)})
        for part in absolute.parts[1:]:
            if part != part.rstrip(" .") or ":" in part:
                _fail("UNSAFE_PATH", f"Unsafe Windows path component: {part}")
            stem = part.split(".", 1)[0].upper()
            if stem in device_names:
                _fail("UNSAFE_PATH", f"Reserved Windows device name: {part}")
    return absolute


def _is_reparse(st: os.stat_result) -> bool:
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attribute and getattr(st, "st_file_attributes", 0) & attribute)


def _assert_safe_components(path: Path, *, leaf_kind: str = "file") -> None:
    """Reject symlink/reparse traversal for every existing path component."""

    absolute = _absolute(path)
    components = list(reversed(absolute.parents)) + [absolute]
    for component in components:
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _fail("IO_ERROR", f"Cannot inspect {component}: {exc}", EXIT_IO)
        if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
            _fail("UNSAFE_PATH", f"Symlink or reparse point is not allowed: {component}")

    try:
        parent_st = os.lstat(absolute.parent)
    except OSError as exc:
        _fail("IO_ERROR", f"State directory is unavailable: {exc}", EXIT_IO)
    if not stat.S_ISDIR(parent_st.st_mode):
        _fail("UNSAFE_PATH", f"Parent is not a directory: {absolute.parent}")

    try:
        leaf_st = os.lstat(absolute)
    except FileNotFoundError:
        return
    except OSError as exc:
        _fail("IO_ERROR", f"Cannot inspect {absolute}: {exc}", EXIT_IO)
    expected = stat.S_ISREG if leaf_kind == "file" else stat.S_ISDIR
    if not expected(leaf_st.st_mode):
        _fail("UNSAFE_PATH", f"Expected a regular {leaf_kind}: {absolute}")
    if leaf_kind == "file" and leaf_st.st_nlink != 1:
        _fail("UNSAFE_PATH", f"Hard-linked state files are not allowed: {absolute}")


def _within_root(path: Path, root: Path) -> Path:
    absolute_path = _absolute(path)
    absolute_root = _absolute(root)
    try:
        common = Path(os.path.commonpath([absolute_path, absolute_root]))
    except ValueError:
        _fail("UNSAFE_PATH", f"Path is outside the trusted workspace root: {path}")
    if os.path.normcase(str(common)) != os.path.normcase(str(absolute_root)):
        _fail("UNSAFE_PATH", f"Path is outside the trusted workspace root: {path}")
    _assert_safe_components(absolute_root, leaf_kind="directory")
    _assert_safe_components(absolute_path)
    return absolute_path


def _path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _fail("IO_ERROR", f"Cannot inspect {path}: {exc}", EXIT_IO)
    return True


class _FileLock(AbstractContextManager["_FileLock"]):
    """Cross-platform advisory lock on a stable sidecar inode."""

    def __init__(self, target: Path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.path = target.with_name(target.name + ".lock")
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        _assert_safe_components(self.path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            self.fd = os.open(self.path, flags, 0o600)
            st = os.fstat(self.fd)
            leaf_st = os.lstat(self.path)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                _fail("UNSAFE_PATH", f"Lock is not a regular file: {self.path}")
            if st.st_ino and leaf_st.st_ino and (st.st_dev, st.st_ino) != (leaf_st.st_dev, leaf_st.st_ino):
                _fail("UNSAFE_PATH", f"Lock path changed while opening: {self.path}")
            if st.st_size < 1:
                os.lseek(self.fd, 0, os.SEEK_SET)
                os.write(self.fd, b"\0")
                os.fsync(self.fd)
        except StateGuardError:
            self._close()
            raise
        except OSError as exc:
            self._close()
            _fail("LOCK_IO_ERROR", f"Cannot open lock {self.path}: {exc}", EXIT_IO)

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return self
            except OSError as exc:
                if not self._is_contention(exc):
                    self._close()
                    _fail("LOCK_IO_ERROR", f"Cannot lock {self.path}: {exc}", EXIT_IO)
                if time.monotonic() >= deadline:
                    self._close()
                    _fail(
                        "LOCK_TIMEOUT",
                        f"Timed out acquiring state lock {self.path}: {exc}",
                        EXIT_CONFLICT,
                    )
                time.sleep(0.05)

    @staticmethod
    def _is_contention(exc: OSError) -> bool:
        return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
            exc, "winerror", None
        ) in {33, 36}

    def _try_lock(self) -> None:
        assert self.fd is not None
        if os.name == "nt":
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self.fd is None:
            return
        if os.name == "nt":
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def _close(self) -> OSError | None:
        if self.fd is None:
            return None
        fd = self.fd
        self.fd = None
        try:
            os.close(fd)
        except OSError as exc:
            return exc
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        release_error: OSError | None = None
        try:
            self._unlock()
        except OSError as unlock_error:
            release_error = unlock_error
        finally:
            close_error = self._close()
            if release_error is None:
                release_error = close_error
        if release_error is not None and exc_type is None:
            _fail(
                "LOCK_RELEASE_UNCERTAIN",
                f"State operation finished but lock release could not be confirmed; reload state before retrying: {release_error}",
                EXIT_IO,
            )


def _read_bytes(path: Path, max_bytes: int) -> bytes:
    _assert_safe_components(path)
    try:
        leaf_st = os.lstat(path)
    except FileNotFoundError:
        _fail("NOT_FOUND", f"State file does not exist: {path}", EXIT_IO)
    except OSError as exc:
        _fail("IO_ERROR", f"Cannot inspect {path}: {exc}", EXIT_IO)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        _fail("NOT_FOUND", f"State file does not exist: {path}", EXIT_IO)
    except OSError as exc:
        _fail("IO_ERROR", f"Cannot open {path}: {exc}", EXIT_IO)

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            _fail("UNSAFE_PATH", f"Expected a regular file: {path}")
        if st.st_ino and leaf_st.st_ino and (st.st_dev, st.st_ino) != (leaf_st.st_dev, leaf_st.st_ino):
            _fail("UNSAFE_PATH", f"State path changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(8192, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
    except StateGuardError:
        raise
    except OSError as exc:
        _fail("IO_ERROR", f"Cannot read {path}: {exc}", EXIT_IO)
    finally:
        os.close(fd)

    if len(raw) > max_bytes:
        _fail("STATE_TOO_LARGE", f"JSON exceeds {max_bytes} UTF-8 bytes")
    return raw


def _reject_constant(value: str) -> NoReturn:
    _fail("INVALID_JSON", f"Non-finite number is not valid JSON: {value}")


def _parse_integer(value: str) -> int:
    parsed = int(value)
    if abs(parsed) > MAX_INTEGER:
        _fail("INTEGER_OUT_OF_RANGE", "Integer exceeds the interoperable JSON range")
    return parsed


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _fail("NON_FINITE_NUMBER", f"Non-finite number is not valid JSON: {value}")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_KEY", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("INVALID_ENCODING", f"{label} must be UTF-8 without BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        _fail("INVALID_ENCODING", f"{label} is not strict UTF-8: {exc}")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=_parse_integer,
            parse_float=_parse_float,
        )
    except StateGuardError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        _fail("INVALID_JSON", f"Cannot parse {label}: {exc}")
    if not isinstance(value, dict):
        _fail("INVALID_ROOT", f"{label} root must be a JSON object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        _fail("INVALID_STATE", f"State cannot be encoded as strict JSON: {exc}")


def _walk_json(value: Any, *, depth: int = 0, allow_null: bool = False) -> None:
    if depth > MAX_DEPTH:
        _fail("NESTING_TOO_DEEP", f"JSON nesting exceeds {MAX_DEPTH}")
    if value is None:
        if allow_null:
            return
        _fail("NULL_NOT_STORABLE", "JSON null is reserved as the patch deletion sentinel")
    if isinstance(value, bool) or isinstance(value, str):
        return
    if type(value) is int:
        if abs(value) > MAX_INTEGER:
            _fail("INTEGER_OUT_OF_RANGE", "Integer exceeds signed 64-bit range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("NON_FINITE_NUMBER", "NaN and Infinity are not allowed")
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            _fail("COLLECTION_TOO_LARGE", "JSON list has too many items")
        for item in value:
            _walk_json(item, depth=depth + 1, allow_null=allow_null)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            _fail("COLLECTION_TOO_LARGE", "JSON object has too many entries")
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("NON_STRING_KEY", "Every JSON object key must be a string")
            _walk_json(item, depth=depth + 1, allow_null=allow_null)
        return
    _fail("INVALID_JSON_TYPE", f"Unsupported JSON value type: {type(value).__name__}")


def _require_token(value: Any, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("INVALID_FIELD", f"{label} must be a nonempty, trimmed string")
    if len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("INVALID_FIELD", f"{label} is too long or contains control characters")
    return value


def _require_text(value: Any, label: str, *, limit: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("INVALID_FIELD", f"{label} must be a nonempty string")
    if len(value) > limit:
        _fail("INVALID_FIELD", f"{label} exceeds {limit} characters")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1 or value > MAX_INTEGER:
        _fail("INVALID_FIELD", f"{label} must be a positive integer, not a boolean")
    return value


def _require_timestamp(value: Any, label: str, *, canonical: bool = False) -> str:
    pattern = (
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
        if canonical
        else r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
    )
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        _fail("INVALID_TIMESTAMP", f"{label} must be UTC RFC3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        _fail("INVALID_TIMESTAMP", f"{label} is invalid: {exc}")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail("INVALID_TIMESTAMP", f"{label} must be UTC")
    return value


def validate_state(state: dict[str, Any], max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """Validate the complete merged candidate before it can be committed."""

    if not isinstance(state, dict):
        _fail("INVALID_ROOT", "State root must be a JSON object")
    _walk_json(state)
    keys = set(state)
    missing = REQUIRED_KEYS - keys
    extra = keys - REQUIRED_KEYS - OPTIONAL_KEYS
    if missing:
        _fail("MISSING_FIELD", f"Missing required state keys: {sorted(missing)}")
    if extra:
        _fail("UNKNOWN_FIELD", f"Unknown top-level state keys: {sorted(extra)}")
    if state["schema_version"] != SCHEMA_VERSION or type(state["schema_version"]) is not int:
        _fail("SCHEMA_VERSION", f"schema_version must be integer {SCHEMA_VERSION}")

    _require_token(state["task_id"], "task_id")
    _require_token(state["owner"], "owner")
    _require_text(state["objective"], "objective")
    objective_revision = _require_positive_int(state["objective_revision"], "objective_revision")
    _require_positive_int(state["state_revision"], "state_revision")
    _require_timestamp(state["updated_at"], "updated_at", canonical=True)

    criteria = state["success_criteria"]
    if not isinstance(criteria, dict) or not criteria:
        _fail("INVALID_CRITERIA", "success_criteria must be a nonempty object")
    if len(criteria) > 128:
        _fail("INVALID_CRITERIA", "success_criteria has more than 128 entries")
    for criterion_id, criterion in criteria.items():
        _require_token(criterion_id, "criterion id", limit=128)
        if not isinstance(criterion, dict):
            _fail("INVALID_CRITERION", f"Criterion {criterion_id} must be an object")
        unknown = set(criterion) - CRITERION_KEYS
        required = {"statement", "status"} - set(criterion)
        if unknown or required:
            _fail(
                "INVALID_CRITERION",
                f"Criterion {criterion_id} has unknown {sorted(unknown)} or missing {sorted(required)} keys",
            )
        _require_text(criterion["statement"], f"criterion {criterion_id} statement", limit=2048)
        if not isinstance(criterion["status"], str) or criterion["status"] not in STATUSES:
            _fail("INVALID_STATUS", f"Criterion {criterion_id} status must be one of {sorted(STATUSES)}")
        evidence = criterion.get("evidence_ref")
        if criterion["status"] == "passed" and evidence is None:
            _fail("MISSING_EVIDENCE", f"Passed criterion {criterion_id} requires evidence_ref")
        if evidence is not None:
            if not isinstance(evidence, dict):
                _fail("INVALID_EVIDENCE", f"Criterion {criterion_id} evidence_ref must be an object")
            evidence_keys = set(evidence)
            if EVIDENCE_REQUIRED_KEYS - evidence_keys or evidence_keys - EVIDENCE_REQUIRED_KEYS - EVIDENCE_OPTIONAL_KEYS:
                _fail("INVALID_EVIDENCE", f"Criterion {criterion_id} evidence_ref has invalid keys")
            _require_text(evidence["ref"], f"criterion {criterion_id} evidence ref", limit=2048)
            _require_text(evidence["scope"], f"criterion {criterion_id} evidence scope", limit=1024)
            evidence_revision = _require_positive_int(
                evidence["objective_revision"],
                f"criterion {criterion_id} evidence objective_revision",
            )
            if evidence_revision != objective_revision:
                _fail("STALE_EVIDENCE", f"Criterion {criterion_id} evidence is from another objective revision")
            if "source" in evidence:
                _require_text(evidence["source"], f"criterion {criterion_id} evidence source", limit=1024)
            if "observed_at" in evidence:
                _require_timestamp(evidence["observed_at"], f"criterion {criterion_id} evidence observed_at")

    if not isinstance(state["facts"], dict):
        _fail("INVALID_FIELD", "facts must be an object")
    if not isinstance(state["pending"], list) or len(state["pending"]) > 256:
        _fail("INVALID_FIELD", "pending must be a list with at most 256 items")
    for object_field in ("decisions", "artifacts", "risks", "uncertainties", "retry_counts"):
        if object_field in state and not isinstance(state[object_field], dict):
            _fail("INVALID_FIELD", f"{object_field} must be an object")
    if "retry_counts" in state:
        for operation, count in state["retry_counts"].items():
            _require_token(operation, "retry_counts key", limit=256)
            if type(count) is not int or count < 0 or count > MAX_INTEGER:
                _fail("INVALID_FIELD", f"retry_counts[{operation}] must be a nonnegative integer")
    if "phase" in state and not isinstance(state["phase"], (str, int)):
        _fail("INVALID_FIELD", "phase must be a string or integer")
    if "phase" in state and isinstance(state["phase"], bool):
        _fail("INVALID_FIELD", "phase boolean is not an integer phase")
    if "last_observation" in state and not isinstance(state["last_observation"], (str, dict, list)):
        _fail("INVALID_FIELD", "last_observation must be a string, object, or list")

    encoded = _canonical_bytes(state)
    if len(encoded) > max_bytes:
        _fail("STATE_TOO_LARGE", f"Canonical state exceeds {max_bytes} UTF-8 bytes")


def load_state(path: os.PathLike[str] | str, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    target = _absolute(path)
    state = _decode_object(_read_bytes(target, max_bytes), "state")
    validate_state(state, max_bytes)
    return state


def merge_patch(
    state: dict[str, Any], patch: dict[str, Any], *, _path: str = "$"
) -> dict[str, Any]:
    """Recursively merge maps; replace lists/scalars; use null to delete a key."""

    if not isinstance(state, dict) or not isinstance(patch, dict):
        _fail("INVALID_PATCH", "State and patch roots must be objects")
    _walk_json(patch, allow_null=True)
    merged = copy.deepcopy(state)
    for key, value in patch.items():
        child_path = f"{_path}.{key}"
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict):
            if key in merged and not isinstance(merged[key], dict):
                _fail(
                    "TYPE_CONFLICT",
                    f"Object patch would reinterpret a non-object value at {child_path}",
                )
            merged[key] = merge_patch(merged.get(key, {}), value, _path=child_path)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def new_state(
    *,
    task_id: str,
    owner: str,
    objective: str,
    criteria: dict[str, str],
    updated_at: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    document = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "owner": owner,
        "objective": objective,
        "objective_revision": 1,
        "state_revision": 1,
        "updated_at": updated_at or _utc_now(),
        "success_criteria": {
            criterion_id: {"statement": statement, "status": "unknown"}
            for criterion_id, statement in criteria.items()
        },
        "facts": {},
        "pending": [],
    }
    validate_state(document, max_bytes)
    return document


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        _fail(
            "COMMIT_UNCERTAIN",
            f"State was replaced but directory durability could not be confirmed: {exc}",
            EXIT_IO,
        )


def _atomic_write(path: Path, state: dict[str, Any], max_bytes: int) -> None:
    data = _canonical_bytes(state)
    if len(data) > max_bytes:
        _fail("STATE_TOO_LARGE", f"Canonical state exceeds {max_bytes} UTF-8 bytes")
    _assert_safe_components(path)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_components(path)
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except StateGuardError:
        raise
    except OSError as exc:
        _fail("ATOMIC_WRITE_FAILED", f"Atomic state write failed: {exc}", EXIT_IO)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def init_state(
    path: os.PathLike[str] | str,
    state: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    target = _absolute(path)
    validate_state(state, max_bytes)
    if state["state_revision"] != 1 or state["objective_revision"] != 1:
        _fail("INVALID_INITIAL_REVISION", "Initial state and objective revisions must both be 1")
    with _FileLock(target, lock_timeout):
        _assert_safe_components(target)
        if _path_exists(target):
            _fail("ALREADY_EXISTS", f"Refusing to overwrite existing state: {target}", EXIT_CONFLICT)
        _atomic_write(target, state, max_bytes)
    return copy.deepcopy(state)


def check_state(
    path: os.PathLike[str] | str,
    *,
    task_id: str | None = None,
    owner: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    state = load_state(path, max_bytes)
    if task_id is not None and state["task_id"] != task_id:
        _fail("TASK_MISMATCH", "State belongs to a different task", EXIT_CONFLICT)
    if owner is not None and state["owner"] != owner:
        _fail("OWNER_MISMATCH", "State has a different canonical owner", EXIT_CONFLICT)
    return state


def _check_mutation_identity(
    state: dict[str, Any], *, task_id: str, actor: str, expected_revision: int
) -> None:
    _require_token(task_id, "task_id")
    _require_token(actor, "actor")
    _require_positive_int(expected_revision, "expected_revision")
    if state["task_id"] != task_id:
        _fail("TASK_MISMATCH", "State belongs to a different task", EXIT_CONFLICT)
    if state["owner"] != actor:
        _fail("OWNER_MISMATCH", "Only the canonical owner may mutate state", EXIT_CONFLICT)
    if state["state_revision"] != expected_revision:
        _fail(
            "STALE_REVISION",
            f"Expected state revision {expected_revision}, found {state['state_revision']}",
            EXIT_CONFLICT,
        )


def _requirements_signature(state: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    objective = state.get("objective")
    criteria = state.get("success_criteria")
    if not isinstance(objective, str) or not isinstance(criteria, dict) or not criteria:
        return None
    statements: list[tuple[str, str]] = []
    for criterion_id, criterion in criteria.items():
        if (
            not isinstance(criterion_id, str)
            or not isinstance(criterion, dict)
            or not isinstance(criterion.get("statement"), str)
        ):
            return None
        statements.append((criterion_id, criterion["statement"]))
    return objective, tuple(sorted(statements))


def apply_state(
    path: os.PathLike[str] | str,
    patch: dict[str, Any],
    *,
    task_id: str,
    actor: str,
    expected_revision: int,
    new_objective_revision: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    target = _absolute(path)
    if not isinstance(patch, dict):
        _fail("INVALID_PATCH", "Patch root must be an object")
    _walk_json(patch, allow_null=True)
    protected = PROTECTED_PATCH_KEYS & set(patch)
    if protected:
        _fail("PROTECTED_FIELD", f"Patch cannot modify protected fields: {sorted(protected)}")

    with _FileLock(target, lock_timeout):
        _assert_safe_components(target)
        current = load_state(target, max_bytes)
        _check_mutation_identity(
            current,
            task_id=task_id,
            actor=actor,
            expected_revision=expected_revision,
        )
        candidate = merge_patch(current, patch)
        current_requirements = _requirements_signature(current)
        candidate_requirements = _requirements_signature(candidate)
        requirements_changed = (
            candidate_requirements is not None
            and candidate_requirements != current_requirements
        )
        if new_objective_revision is None:
            target_objective_revision = current["objective_revision"]
        else:
            _require_positive_int(new_objective_revision, "new_objective_revision")
            if new_objective_revision != current["objective_revision"] + 1:
                _fail(
                    "OBJECTIVE_REVISION_SEQUENCE",
                    "new_objective_revision must increment the current objective revision by exactly 1",
                    EXIT_CONFLICT,
                )
            target_objective_revision = new_objective_revision

        if requirements_changed and new_objective_revision is None:
            _fail(
                "OBJECTIVE_REVISION_REQUIRED",
                "Changing objective or criterion IDs/statements requires --new-objective-revision",
                EXIT_CONFLICT,
            )
        candidate["objective_revision"] = target_objective_revision
        if candidate == current:
            return current
        candidate["state_revision"] = current["state_revision"] + 1
        candidate["updated_at"] = _utc_now()
        validate_state(candidate, max_bytes)
        _atomic_write(target, candidate, max_bytes)
        return candidate


def handoff_state(
    path: os.PathLike[str] | str,
    *,
    task_id: str,
    actor: str,
    new_owner: str,
    expected_revision: int,
    max_bytes: int = DEFAULT_MAX_BYTES,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    target = _absolute(path)
    _require_token(new_owner, "new_owner")
    with _FileLock(target, lock_timeout):
        _assert_safe_components(target)
        current = load_state(target, max_bytes)
        _check_mutation_identity(
            current,
            task_id=task_id,
            actor=actor,
            expected_revision=expected_revision,
        )
        if new_owner == current["owner"]:
            return current
        candidate = copy.deepcopy(current)
        candidate["owner"] = new_owner
        candidate["state_revision"] = current["state_revision"] + 1
        candidate["updated_at"] = _utc_now()
        validate_state(candidate, max_bytes)
        _atomic_write(target, candidate, max_bytes)
        return candidate


def completion_errors(
    state: dict[str, Any], max_bytes: int = DEFAULT_MAX_BYTES
) -> list[str]:
    validate_state(state, max_bytes)
    errors: list[str] = []
    for criterion_id, criterion in state["success_criteria"].items():
        if criterion["status"] != "passed":
            errors.append(f"{criterion_id}: status is {criterion['status']}")
        elif "evidence_ref" not in criterion:
            errors.append(f"{criterion_id}: evidence_ref is missing")
    return errors


def _load_patch(path: Path, max_bytes: int) -> dict[str, Any]:
    patch = _decode_object(_read_bytes(path, max_bytes), "patch")
    _walk_json(patch, allow_null=True)
    return patch


def _criteria(values: list[str]) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            _fail("INVALID_CRITERION", "Each --criterion must be ID=statement")
        criterion_id, statement = raw.split("=", 1)
        if criterion_id in criteria:
            _fail("INVALID_CRITERION", f"Duplicate criterion id: {criterion_id}")
        criteria[criterion_id] = statement
    return criteria


def _summary(
    state: dict[str, Any], *, changed: bool, path: Path, max_bytes: int
) -> dict[str, Any]:
    errors = completion_errors(state, max_bytes)
    return {
        "ok": True,
        "changed": changed,
        "path": str(path),
        "task_id": state["task_id"],
        "owner": state["owner"],
        "objective_revision": state["objective_revision"],
        "state_revision": state["state_revision"],
        "completion_ready": not errors,
        "completion_errors": errors,
    }


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _fail("USAGE", message, EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("state", type=Path)
        subparser.add_argument("--root", type=Path, default=Path.cwd())
        subparser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)

    init = subparsers.add_parser("init")
    common(init)
    init.add_argument("--task-id", required=True)
    init.add_argument("--owner", required=True)
    init.add_argument("--objective", required=True)
    init.add_argument("--criterion", action="append", required=True)

    check = subparsers.add_parser("check")
    common(check)
    check.add_argument("--task-id")
    check.add_argument("--owner")

    apply = subparsers.add_parser("apply")
    common(apply)
    apply.add_argument("patch", type=Path)
    apply.add_argument("--task-id", required=True)
    apply.add_argument("--owner", required=True)
    apply.add_argument("--expected-revision", required=True, type=int)
    apply.add_argument("--new-objective-revision", type=int)

    handoff = subparsers.add_parser("handoff")
    common(handoff)
    handoff.add_argument("--task-id", required=True)
    handoff.add_argument("--owner", required=True)
    handoff.add_argument("--new-owner", required=True)
    handoff.add_argument("--expected-revision", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.max_bytes < 1024:
            _fail("INVALID_LIMIT", "--max-bytes must be at least 1024")
        state_path = _within_root(args.state, args.root)
        if args.command == "init":
            document = new_state(
                task_id=args.task_id,
                owner=args.owner,
                objective=args.objective,
                criteria=_criteria(args.criterion),
                max_bytes=args.max_bytes,
            )
            result = init_state(state_path, document, max_bytes=args.max_bytes)
            changed = True
        elif args.command == "check":
            result = check_state(
                state_path,
                task_id=args.task_id,
                owner=args.owner,
                max_bytes=args.max_bytes,
            )
            changed = False
        elif args.command == "apply":
            patch_path = _within_root(args.patch, args.root)
            patch = _load_patch(patch_path, args.max_bytes)
            result = apply_state(
                state_path,
                patch,
                task_id=args.task_id,
                actor=args.owner,
                expected_revision=args.expected_revision,
                new_objective_revision=args.new_objective_revision,
                max_bytes=args.max_bytes,
            )
            changed = result["state_revision"] != args.expected_revision
        else:
            result = handoff_state(
                state_path,
                task_id=args.task_id,
                actor=args.owner,
                new_owner=args.new_owner,
                expected_revision=args.expected_revision,
                max_bytes=args.max_bytes,
            )
            changed = result["state_revision"] != args.expected_revision
        print(
            json.dumps(
                _summary(
                    result,
                    changed=changed,
                    path=state_path,
                    max_bytes=args.max_bytes,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    except StateGuardError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "message": exc.message},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
