# ABOUTME: Official-verification sandbox (Linux bubblewrap): no network, read-only system/toolchain/deps, one rw
# ABOUTME: workdir mounted at the canonical build path, small tmpfs only, env allowlist, safety limits; `none` elsewhere.
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .runtime import benchkit as bk

# The only variables a sandboxed toolchain process sees; LEAN_PATH is set by `lake env` inside.
ENV_ALLOWLIST = ("PATH", "HOME", "LEAN_ABORT_ON_PANIC", "LEAN_PATH", "LD_LIBRARY_PATH")
SANDBOX_HOME = "/tmp/home"
# tmpfs in the sandbox live in RAM that the memory watchdog does not see (files are not process memory), so they are
# small: /tmp (with HOME) and /dev/shm; /dev itself is read-only (its device files still work).
TMP_MB = 512
DEV_SHM_MB = 64


def bwrap_binary() -> str | None:
    found = os.environ.get("SBX_BWRAP") or shutil.which("bwrap")
    return found if found and Path(found).is_file() else None


def choose(requested: str) -> str:
    """`auto` means bwrap when this is Linux and bwrap exists, else none."""
    if requested == "auto":
        return "bwrap" if sys.platform.startswith("linux") and bwrap_binary() else "none"
    if requested == "bwrap" and not bwrap_binary():
        raise SystemExit("--sandbox bwrap: no bwrap binary (set SBX_BWRAP or put bwrap on PATH)")
    return requested


class BwrapRunner(bk.LocalRunner):
    """Stages the project in <workdir>/build/project and shows it inside the sandbox at the canonical build path,
    so the .olean bytes equal those of an unsandboxed macOS build of the same sources."""
    sandbox = "bwrap"

    def __init__(self, toolchain: Path, task_sha: str, workdir: Path, deps: Path | None, mem_mb: int | None = None):
        self.toolchain = toolchain
        self.base = workdir / "build"
        self.project = self.base / "project"
        self.inside_project = bk.CANONICAL_BUILD_ROOT / task_sha[:12] / "project"
        self.deps = deps.resolve() if deps else None
        self.mem_mb = mem_mb
        self.max_processes: int | None = None
        self.disk_mb: int | None = None
        self.exposed: list[Path] = []
        self.stats: dict = {}

    def __enter__(self):
        self.base.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.project, ignore_errors=True)

    def expose(self, path: Path) -> None:
        """Organizer directories (the compiled metric tool) are mounted read-only at their host paths."""
        self.exposed.append(Path(path).resolve())

    def argv(self, cmd: list[str], extra_env: dict | None = None) -> list[str]:
        a = [bwrap_binary(), "--unshare-all", "--die-with-parent", "--new-session",
             "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
             "--proc", "/proc", "--dev", "/dev", "--size", str(DEV_SHM_MB << 20), "--tmpfs", "/dev/shm",
             "--remount-ro", "/dev", "--size", str(TMP_MB << 20), "--tmpfs", "/tmp",
             "--ro-bind", str(self.toolchain), str(self.toolchain),
             "--bind", str(self.project), str(self.inside_project)]
        for p in ("/bin", "/lib", "/lib64", "/sbin"):
            if os.path.islink(p):
                a += ["--symlink", os.readlink(p), p]
            elif os.path.isdir(p):
                a += ["--ro-bind", p, p]
        if self.deps:
            a += ["--ro-bind", str(self.deps), str(self.deps)]
        for p in self.exposed:
            a += ["--ro-bind", str(p), str(p)]
        env = {"PATH": f"{self.toolchain / 'bin'}:/usr/bin:/bin", "HOME": SANDBOX_HOME}
        env.update({k: v for k, v in (extra_env or {}).items()})
        a += ["--dir", SANDBOX_HOME, "--chdir", str(self.inside_project), "--clearenv"]
        for k, v in sorted(env.items()):
            a += ["--setenv", k, v]
        return a + ["--"] + cmd

    def run(self, cmd: list[str], timeout: float, extra_env: dict | None = None):
        """The safety limits (wall clock, memory, processes, disk written to the project directory, the only host
        directory the sandbox can write) come from benchkit.run's watchdog on the bwrap process tree, which the pid
        namespace keeps whole; CPU time and core dumps from rlimits that every sandboxed process inherits."""
        return bk.run(self.argv(cmd, extra_env), self.project, None, timeout, self.mem_mb, self.stats,
                      bk.child_rlimits(timeout, self.disk_mb), self.max_processes, self.disk_mb, [self.project])


def factory(kind: str, workdir: Path, deps: Path | None, mem_mb: int | None = None):
    """runner_factory for benchkit.evaluate."""
    if kind == "none":
        return None
    return lambda toolchain, task_sha: BwrapRunner(toolchain, task_sha, workdir, deps, mem_mb)
