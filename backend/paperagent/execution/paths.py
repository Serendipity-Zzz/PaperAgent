from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PathOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"


@dataclass(frozen=True)
class ManagedPathDecision:
    allowed: bool
    requires_approval: bool
    reason: str
    resolved_path: Path


class ManagedPathPolicy:
    """Classify file operations against canonical PaperAgent-managed roots."""

    def __init__(self, *, read_roots: list[Path], write_roots: list[Path]) -> None:
        self.read_roots = tuple(self._canonical(root) for root in read_roots)
        self.write_roots = tuple(self._canonical(root) for root in write_roots)

    @staticmethod
    def _canonical(path: Path) -> Path:
        return Path(os.path.normcase(path.expanduser().resolve(strict=False)))

    @staticmethod
    def _contains(root: Path, target: Path) -> bool:
        return target == root or root in target.parents

    @staticmethod
    def _has_reparse_component(target: Path) -> bool:
        current = target
        while current != current.parent:
            if current.exists():
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    return True
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if attributes & reparse_flag:
                    return True
            current = current.parent
        return False

    def classify(self, path: Path, operation: PathOperation) -> ManagedPathDecision:
        target = self._canonical(path)
        if operation is PathOperation.DELETE:
            return ManagedPathDecision(
                allowed=False,
                requires_approval=True,
                reason="all deletions require a one-shot user approval",
                resolved_path=target,
            )
        if self._has_reparse_component(path.expanduser().absolute()):
            return ManagedPathDecision(
                allowed=False,
                requires_approval=True,
                reason="path contains a symbolic link or reparse point",
                resolved_path=target,
            )
        if operation is PathOperation.WRITE:
            managed = any(self._contains(root, target) for root in self.write_roots)
            return ManagedPathDecision(
                allowed=managed,
                requires_approval=not managed,
                reason=(
                    "write is inside a managed root"
                    if managed
                    else "write is outside PaperAgent-managed roots"
                ),
                resolved_path=target,
            )
        readable = any(
            self._contains(root, target) for root in (*self.read_roots, *self.write_roots)
        )
        return ManagedPathDecision(
            allowed=readable,
            requires_approval=not readable,
            reason="read is in scope" if readable else "read is outside authorized roots",
            resolved_path=target,
        )
