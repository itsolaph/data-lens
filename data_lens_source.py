"""Safe, bounded acquisition of a local data file.

Every entry point (middleware, profile tool, query tool) goes through :func:`load_source`, so the
root allowlist, file-type and protected-name rules are enforced independently of ``read_file``'s own
guards. This is *policy*, not OS containment: it cannot stop a hostile process that rewrites
directories concurrently (documented in the README).

Race handling: the file is opened once (``O_NOFOLLOW``), its identity is checked on the *descriptor*
(regular file, same dev/ino as the resolved path), the bytes are read in one bounded pass, and the
path is re-resolved and size/mtime/inode re-checked afterwards. All
parsing then runs on that immutable in-memory snapshot, so a file changed mid-scan can never produce a
mixed result.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from .data_lens_core import Limits, LensError, Settings, check_deadline

SUPPORTED = {".csv": "csv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}

_DENY_NAMES = {
    ".env", "auth.json", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json", "secret.json", "service-account.json",
    "client_secret.json", "token.json", "config.yaml", "state.db",
}
_DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".gpg", ".ppk")
_DENY_NAME_PARTS = ("secret", "credential", "passwd", "password", "private_key", "private-key", "apikey", "api_key")
_DENY_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".git", "mcp-tokens", "browser-profile", "vault"}
_DENY_PREFIXES = ("/proc", "/sys", "/dev")

_CRED_FIELDS = {
    "password", "passwd", "secret", "client_secret", "private_key", "privatekey", "api_key", "apikey",
    "api_secret", "access_token", "refresh_token", "secret_key", "auth_token", "bearer_token",
    "session_token", "authorization",
}
_CRED_BYTES = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    rb"|sk-ant-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}")


@dataclass(frozen=True)
class Source:
    path: str            # resolved real path
    requested: str       # path as given by the caller
    fmt: str             # csv | json | jsonl
    data: bytes
    size: int
    mtime_ns: int
    sha256: str

    @property
    def fingerprint(self) -> str:
        return "sha256:" + self.sha256


def credential_field_names(names) -> bool:
    return any(str(n).strip().lower().replace("-", "_") in _CRED_FIELDS for n in names)


def _protected_dirs() -> list[str]:
    here = os.path.dirname(os.path.realpath(__file__))
    out = [here]
    for p in (os.environ.get("HERMES_HOME"), os.path.expanduser("~/.hermes")):
        if p:
            out.append(os.path.realpath(p))
    return out


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _check_name(resolved: str, requested: str) -> None:
    """Protected names/dirs, checked on BOTH the requested and the resolved path."""
    for candidate in (resolved, requested):
        p = PurePosixPath(candidate)
        name = p.name.lower()
        if name in _DENY_NAMES or p.stem.lower() in _DENY_NAMES or name.split(".")[0] in _DENY_NAMES \
                or name.startswith(".env") or name.endswith(_DENY_SUFFIXES) \
                or any(part in name for part in _DENY_NAME_PARTS):
            raise LensError("path_not_permitted", "this file name is protected and will not be read")
        if any(part.lower() in _DENY_DIRS for part in p.parts[:-1]):
            raise LensError("path_not_permitted", "this directory is protected and will not be read")
        if any(_under(candidate, pre) for pre in _DENY_PREFIXES):
            raise LensError("path_not_permitted", "system paths are not readable")
    for prot in _protected_dirs():
        if _under(resolved, prot):
            raise LensError("path_not_permitted", "Hermes internal and plugin state is never readable")


def format_for(path: str) -> str:
    fmt = SUPPORTED.get(PurePosixPath(path).suffix.lower())
    if fmt is None:
        raise LensError("unsupported_file_type", "only .csv, .json, .jsonl and .ndjson files are supported")
    return fmt


def backend_is_local() -> bool:
    """Best-effort: every signal we can read says the file tools run on this host.

    Signals: the ``TERMINAL_ENV`` environment variable (Hermes bridges ``terminal.backend`` into it,
    lazily) and ``terminal.backend`` in config. Unreadable config => not verifiably local. This is a
    second line of defence behind the explicit ``host_local`` opt-in, not a proof.
    """
    env = os.environ.get("TERMINAL_ENV", "").strip().lower()
    if env not in ("", "local"):
        return False
    try:
        from hermes_cli.config import load_config_readonly
        term = (load_config_readonly() or {}).get("terminal") or {}
        cfg = str(term.get("backend") or term.get("env_type") or "").strip().lower() if isinstance(term, dict) else ""
    except Exception:
        return False
    return cfg in ("", "local")


def check_path(path: object, settings: Settings) -> tuple[str, str]:
    """Pure path policy (no file content). Returns ``(resolved_path, fmt)``."""
    if not settings.roots:
        raise LensError("roots_not_configured",
                        "no roots are configured; set plugins.entries.data-lens.settings.roots (default: none)")
    if not settings.host_local:
        raise LensError("host_local_not_enabled",
                        "host-filesystem access is off; set plugins.entries.data-lens.settings.host_local to true "
                        "only when the agent's file tools run on this machine")
    if not backend_is_local():
        raise LensError("backend_not_local", "the active terminal backend is not (verifiably) the local host")
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > 4096:
        raise LensError("invalid_path", "path must be a non-empty string")
    if not path.startswith("/"):
        raise LensError("path_not_absolute", "an absolute host path is required (relative or ~ paths are refused)")
    fmt = format_for(path)
    resolved = os.path.realpath(path)
    roots = [os.path.realpath(r) for r in settings.roots]
    if not any(resolved != r and _under(resolved, r) for r in roots):
        raise LensError("outside_roots", "path is outside the configured roots (after resolving symlinks)")
    _check_name(resolved, path)
    format_for(resolved)  # a symlink must not change the effective file type
    return resolved, fmt


def load_source(path: object, settings: Settings, deadline: float | None = None) -> Source:
    check_deadline(deadline)
    limits: Limits = settings.limits
    resolved, fmt = check_path(path, settings)
    try:
        st = os.stat(resolved)
    except OSError:
        raise LensError("not_readable", "file does not exist or cannot be accessed") from None
    if not stat.S_ISREG(st.st_mode):
        raise LensError("not_regular_file", "only regular files are supported (no devices, FIFOs or sockets)")
    if st.st_size > limits.max_bytes:
        raise LensError("file_too_large", "file exceeds the %d byte limit" % limits.max_bytes)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError:
        raise LensError("not_readable", "file cannot be opened (missing, permission, or a symlink swap)") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (st.st_dev, st.st_ino):
            raise LensError("file_changed_during_read", "the file was replaced while it was being opened")
        chunks, total = [], 0
        while True:
            check_deadline(deadline)
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > limits.max_bytes:
                raise LensError("file_too_large", "file exceeds the %d byte limit" % limits.max_bytes)
            chunks.append(chunk)
        after = os.fstat(fd)
        # Re-resolve once more: if any path component was swapped while we read, the path no longer
        # leads to the inode we read (best effort; see "not OS containment" in the README).
        again = os.path.realpath(resolved)
        st2 = os.stat(again)
        if again != resolved or (st2.st_dev, st2.st_ino) != (before.st_dev, before.st_ino):
            raise LensError("file_changed_during_read", "the path now resolves to a different file; retry")
    except OSError:
        raise LensError("not_readable", "file could not be read") from None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino) \
            or len(data) != before.st_size:
        raise LensError("file_changed_during_read", "the file changed while it was being read; retry")
    if _CRED_BYTES.search(data):
        raise LensError("credential_like_content", "file content looks like credentials or key material; refused")
    digest = hashlib.sha256(data).hexdigest()
    check_deadline(deadline)
    return Source(path=resolved, requested=str(path), fmt=fmt, data=data, size=len(data),
                  mtime_ns=before.st_mtime_ns, sha256=digest)
