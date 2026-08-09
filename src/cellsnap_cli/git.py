"""git subprocess plumbing and clone-URL normalization."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

__all__ = ["GitError", "git", "git_out", "git_ok", "git_paths", "remote_to_web"]

# SCP-style remote: [user@]host:path, the one clone URL form that is not a URL.
SCP_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


class GitError(RuntimeError):
    """A git invocation exited non-zero."""


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode:
        raise GitError(f"`git {' '.join(args)}` failed: {proc.stderr.strip()}")
    return proc


def git_out(*args: str, cwd: Path) -> str:
    return git(*args, cwd=cwd).stdout.strip()


def git_ok(*args: str, cwd: Path) -> bool:
    return git(*args, cwd=cwd, check=False).returncode == 0


def git_paths(*args: str, cwd: Path) -> set[str]:
    """Paths from a NUL-separated git command. Pass -z yourself.

    Without -z, git quotes any path holding non-ASCII bytes ("caf\\303\\251.py"), so comparing
    its output against real paths silently misses those files.
    """
    return {p for p in git(*args, cwd=cwd).stdout.split("\0") if p}


def remote_to_web(remote: str) -> str | None:
    """Clone URL -> web base URL. None for local paths and anything unrecognized.

    Git exposes no browse URL. The host comes from the remote, so self-hosted instances work.
    """
    r = remote.strip()
    if not r or r.startswith((".", "/", "file://")):
        return None

    if "://" in r:
        parsed = urlparse(r)
        if parsed.scheme not in {"http", "https", "ssh", "git"}:
            return None
        host, path = parsed.hostname, parsed.path
    else:
        m = SCP_RE.match(r)
        if not m:
            return None
        host, path = m.group("host"), m.group("path")

    if not host or ("." not in host and host != "localhost"):
        return None  # Windows drive letters and other non-hostnames

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"https://{host}/{path}" if path else None
