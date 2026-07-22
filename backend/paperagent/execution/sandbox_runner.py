from __future__ import annotations

import os
import runpy
import sys
import tempfile
from pathlib import Path
from typing import cast


class SandboxViolation(PermissionError):
    pass


def _inside(root: Path, value: object) -> bool:
    if isinstance(value, int):
        return True
    if not isinstance(value, (str, os.PathLike)):
        return False
    try:
        path_value = cast(str | os.PathLike[str], value)
        target = Path(os.fspath(path_value)).resolve()
    except TypeError:
        return False
    return target == root or root in target.parents


def _is_write_open(arguments: tuple[object, ...]) -> bool:
    mode = arguments[1] if len(arguments) > 1 else None
    flags = arguments[2] if len(arguments) > 2 else 0
    if isinstance(mode, str) and any(marker in mode for marker in ("w", "a", "+", "x")):
        return True
    if isinstance(flags, int):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        return bool(flags & write_flags)
    return False


def install_audit_guard(root: Path) -> None:
    root = root.resolve()
    deletion_events = {
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.replace",
        "shutil.rmtree",
    }
    process_events = {"os.system", "os.spawn", "subprocess.Popen"}
    network_events = {"socket.connect", "socket.bind", "socket.getaddrinfo"}

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event in deletion_events:
            raise SandboxViolation(f"deletion or replacement requires approval: {event}")
        if event in process_events:
            raise SandboxViolation(f"nested process execution is blocked: {event}")
        if event in network_events:
            raise SandboxViolation(f"network access is not authorized: {event}")
        if (
            event == "open"
            and arguments
            and _is_write_open(arguments)
            and not _inside(root, arguments[0])
        ):
            raise SandboxViolation("write target is outside the managed run workspace")
        if event in {"os.mkdir", "os.symlink", "os.link"} and arguments:
            target = arguments[1] if event in {"os.symlink", "os.link"} else arguments[0]
            if not _inside(root, target):
                raise SandboxViolation("filesystem target is outside the managed run workspace")

    sys.addaudithook(audit)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 4 or values[0] != "--root" or "--" not in values:
        raise SystemExit("usage: sandbox_runner.py --root WORKSPACE -- SCRIPT [ARGS...]")
    separator = values.index("--")
    root = Path(values[1]).resolve()
    script_arguments = values[separator + 1 :]
    if not script_arguments:
        raise SystemExit("a Python script is required")
    script = (root / script_arguments[0]).resolve()
    if not _inside(root, script) or not script.is_file():
        raise SandboxViolation("script is outside the managed run workspace")
    # Python's default temp-dir discovery creates and then deletes probe files. Deletion is
    # intentionally unavailable in this sandbox, so provide a pre-declared managed scratch
    # directory instead of weakening the audit policy.
    scratch = root / ".scratch"
    matplotlib_cache = scratch / "matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    os.environ.update(
        {
            "TMP": str(scratch),
            "TEMP": str(scratch),
            "TMPDIR": str(scratch),
            "MPLCONFIGDIR": str(matplotlib_cache),
        }
    )
    install_audit_guard(root)
    sys.path.insert(0, str(root))
    sys.argv = [str(script), *script_arguments[1:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
