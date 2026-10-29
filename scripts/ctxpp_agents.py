"""Non-destructive management of ctxpp's repository routing block."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

START = b"<!-- agent-interface:ctxpp:start -->"
END = b"<!-- agent-interface:ctxpp:end -->"
MARKER_RE = re.compile(rb"<!--\s*agent-interface:ctxpp:[^>]*-->")
MARKER_MENTION_RE = re.compile(rb"<!--\s*agent-interface:ctxpp\b")


class AgentInstructionsError(RuntimeError):
    """The repository's AGENTS.md cannot be safely updated for ctxpp."""


def _section(newline: bytes) -> bytes:
    lines = (
        START,
        b"This repository uses ctxpp as its canonical C++ context interface.",
        b"Route C++ symbol lookup, navigation, dependency tracing, and source-context requests through `ctxpp`.",
        b"Canonical source files remain authoritative; `.ctxpp/` contains generated retrieval artifacts.",
        b"Consider installing the cpp-context-compiler skill from https://github.com/tumlinso/ctxpp; if installed, read its `SKILL.md` before use.",
        END,
    )
    return newline.join(lines)


def prepare_agent_instructions(root: Path) -> tuple[Path, bytes | None, bytes] | None:
    """Validate the marker layout and produce an exact, non-mutating update."""
    path = root / "AGENTS.md"
    if path.is_symlink():
        raise AgentInstructionsError("AGENTS.md is a symbolic link; refusing to modify it")
    if path.exists() and not path.is_file():
        raise AgentInstructionsError("AGENTS.md is not a regular file; refusing to modify it")
    try:
        before = path.read_bytes() if path.exists() else None
    except OSError as exc:
        raise AgentInstructionsError(f"cannot read AGENTS.md: {exc}") from exc
    content = before if before is not None else b""
    markers = list(MARKER_RE.finditer(content))
    mentions = list(MARKER_MENTION_RE.finditer(content))
    if len(mentions) != len(markers):
        raise AgentInstructionsError(
            "AGENTS.md has a partial ctxpp interface marker; repair it before running init"
        )
    if markers:
        if (len(markers) != 2 or markers[0].group(0) != START
                or markers[1].group(0) != END or markers[0].start() >= markers[1].start()):
            raise AgentInstructionsError(
                "AGENTS.md has duplicate or malformed ctxpp interface markers; repair them before running init"
            )
        start, end = markers[0], markers[1]
        newline = b"\r\n" if b"\r\n" in content else b"\n"
        replacement = _section(newline)
        after = content[:start.start()] + replacement + content[end.end():]
    else:
        newline = b"\r\n" if b"\r\n" in content else b"\n"
        section = _section(newline)
        if not content:
            after = section + newline
        else:
            separator = newline + newline
            after = content + separator + section + newline
    if before == after:
        return None
    return path, before, after


def write_agent_instructions(update: tuple[Path, bytes | None, bytes] | None) -> bool:
    """Atomically apply an update while preserving the file mode and concurrent edits."""
    if update is None:
        return False
    path, expected, content = update
    current = path.read_bytes() if path.exists() else None
    if path.is_symlink() or current != expected:
        raise AgentInstructionsError("AGENTS.md changed during initialization; rerun ctxpp init to update it safely")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".AGENTS.md.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


__all__ = ["AgentInstructionsError", "prepare_agent_instructions", "write_agent_instructions"]
