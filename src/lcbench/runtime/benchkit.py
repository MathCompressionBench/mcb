# ABOUTME: Participant runtime for lcbench task packages (stdlib-only Python): start, check, submit.
# ABOUTME: Shipped inside every package and imported by the verifier, so the local check is the official check.
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD_TIMEOUT_S = 3 * 3600
SUBMISSION_SCHEMA = "lcbench.submission.v1"
TASK_SCHEMA = "lcbench.task.v1"
MAX_ARCHIVE_BYTES = 1 << 30
CHECK_SCHEMA = "lcbench.check.v1"
# semver of the scorer (this file and the organizer Lean tool sources); printed by ./check and recorded in
# TASK.json and every receipt. Every change to those files is a new version (tests/unit/test_scorer_versions.py):
# a package built by one scorer is rejected by a verifier running another.
SCORER_VERSION = "1.0.0-rc.11"
# metrics every check and receipt reports next to the primary one (null until the scorer computes them)
RECORDED_METRICS = ["dag_nodes_v1"]


# ---------------------------------------------------------------- hashing and canonical files

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def pretty_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def list_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def write_manifest(root: Path, name: str = "MANIFEST.sha256") -> Path:
    lines = [f"{sha256_file(root / rel)}  {rel}\n" for rel in list_files(root) if rel != name]
    out = root / name
    out.write_text("".join(lines))
    return out


def check_manifest(root: Path, name: str = "MANIFEST.sha256") -> list[str]:
    """Every file under root must be listed with a matching hash, and nothing listed may be missing."""
    mpath = root / name
    if not mpath.is_file():
        return [f"{name} missing"]
    problems, listed = [], {}
    for line in mpath.read_text().splitlines():
        digest, _, rel = line.partition("  ")
        listed[rel] = digest
    present = [rel for rel in list_files(root) if rel != name]
    for rel in present:
        if rel not in listed:
            problems.append(f"file not in manifest: {rel}")
        elif sha256_file(root / rel) != listed[rel]:
            problems.append(f"hash mismatch (file modified after hashing): {rel}")
    for rel in listed:
        if rel not in present:
            problems.append(f"manifest lists missing file: {rel}")
    return problems


def tree_sha256(root: Path, files: list[str]) -> str:
    return sha256_bytes("".join(f"{sha256_file(root / rel)}  {rel}\n" for rel in sorted(files)).encode())


def deterministic_tar(src_dir: Path, out_path: Path, arcroot: str) -> str:
    """Tar+gzip with sorted entries, zero mtimes/owners, so equal trees give equal bytes; returns sha256."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            files = list_files(src_dir)
            dirs = sorted({p.as_posix() for rel in files for p in Path(rel).parents if p.as_posix() != "."})
            for d in [""] + dirs:
                info = tarfile.TarInfo(f"{arcroot}/{d}".rstrip("/"))
                info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
                tar.addfile(info)
            for rel in files:
                data = (src_dir / rel).read_bytes()
                info = tarfile.TarInfo(f"{arcroot}/{rel}")
                info.size, info.mtime = len(data), 0
                info.mode = 0o755 if os.access(src_dir / rel, os.X_OK) else 0o644
                tar.addfile(info, io.BytesIO(data))
    out_path.write_bytes(buf.getvalue())
    return sha256_bytes(buf.getvalue())


def safe_extract(tar_path: Path, dest: Path, max_bytes: int = MAX_ARCHIVE_BYTES) -> Path:
    """Extract only regular files/dirs with relative, non-escaping names; returns the single top directory."""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    tops = set()
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts or not parts:
                raise ValueError(f"unsafe path in archive: {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"non-regular archive member: {m.name!r}")
            total += m.size
            if total > max_bytes:
                raise ValueError("archive too large")
            tops.add(parts[0])
            target = dest / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
            target.chmod(0o755 if m.mode & 0o100 else 0o644)
    if len(tops) != 1:
        raise ValueError(f"archive must have one top-level directory, found {sorted(tops)}")
    return dest / tops.pop()


# ---------------------------------------------------------------- Lean toolchain and build

class InfrastructureStop(Exception):
    """A safety limit stopped a command's process tree: memory (`mem_mb`), process count (`max_processes`) or disk
    (`disk_mb`). These limits protect the machine and say nothing about the submission: the evaluation ends without a
    verdict and is re-run on another machine. `limit` names the TASK.json `limits` key."""
    limit = ""


class MemoryLimitExceeded(InfrastructureStop):
    """The tree's memory reached the stop point (MEM_STOP_FRACTION of `mem_mb`); the tree was killed."""
    limit = "mem_mb"


class ProcessLimitExceeded(InfrastructureStop):
    """The tree had more than `max_processes` processes; the tree was killed."""
    limit = "max_processes"


class DiskLimitExceeded(InfrastructureStop):
    """The command's writable directories held at least `disk_mb`; the tree was killed."""
    limit = "disk_mb"


# The watchdog samples a running command's process tree at most WATCH_INTERVAL_S apart, and sooner as the tree nears a
# cap: the next sample comes before the tree could reach the memory stop point (MEM_STOP_FRACTION of `mem_mb`) growing
# at ALLOC_RATE_MB_S, or `max_processes` growing at SPAWN_RATE_PER_S, but never sooner than MIN_WATCH_INTERVAL_S after
# the previous one. ALLOC_RATE_MB_S is three times what one process allocated and touched on the fastest cluster node
# (10.7 GB/s; four processes on four CPUs reached 36.5 GB/s together). At that rate the shortest interval adds
# 160 MB, less than half of the margin above the stop point for the smallest task limit (10% of 4096 MB).
# SPAWN_RATE_PER_S is above what the adversarial probes reached (about 700 processes in well under a second).
WATCH_INTERVAL_S = 0.5
MIN_WATCH_INTERVAL_S = 0.005
ALLOC_RATE_MB_S = 32768
SPAWN_RATE_PER_S = 10000
# The tree is stopped when its memory passes this fraction of `mem_mb`: a margin for the time a sample and a kill take.
MEM_STOP_FRACTION = 0.9
# The writable directories are measured at most this often (a walk of their files), and never more often than
# DISK_WALK_SHARE of the time (a slow walk of a large build directory spaces the walks out).
DISK_WATCH_INTERVAL_S = 0.25
DISK_WALK_SHARE = 0.05
# Of each of a command's stdout and stderr, only the last OUTPUT_TAIL_BYTES are kept in this process. They are for logs:
# a result the verifier reads (the extractor's facts, the counter's metrics) goes to a file.
OUTPUT_TAIL_BYTES = 16 << 20


def process_table() -> dict[int, tuple[int, int]]:
    """{pid: (parent pid, resident KiB)} of every process visible on this machine (/proc on Linux, ps elsewhere)."""
    table = {}
    if sys.platform.startswith("linux"):
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            table[int(d)] = (int(fields[1]), int(fields[21]) * page_kb)
        return table
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(x.isdigit() for x in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]))
    return table


def memory_source() -> str:
    """Name of the memory measure the watchdog applies to a process tree: `private+files_once` on Linux and macOS,
    `rss_sum` elsewhere. private+files_once = the private memory of every process (Linux: RssAnon + RssShmem from
    /proc/<pid>/status; macOS: phys_footprint from proc_pid_rusage) plus the file-backed resident pages of the process
    that maps the most, counted once. Parallel Lean processes map the same .olean files, so summing resident sets
    counts those pages once per process; this measure counts them once. For a single process it is its resident set,
    and it does not depend on what other processes on the machine map. rss_sum sums the resident sets."""
    return "private+files_once" if sys.platform.startswith("linux") or sys.platform == "darwin" else "rss_sum"


def _linux_split_kb(pid: int) -> tuple[int, int] | None:
    """(RssAnon + RssShmem, RssFile) of a process in KiB; (0, 0) once it has exited; None if unreadable."""
    try:
        with open(f"/proc/{pid}/status") as f:
            fields = dict(line.split(":", 1) for line in f if line.startswith("Rss"))
        return (int(fields["RssAnon"].split()[0]) + int(fields["RssShmem"].split()[0]),
                int(fields["RssFile"].split()[0]))
    except (FileNotFoundError, ProcessLookupError):
        return 0, 0
    except (OSError, KeyError, ValueError):
        return None


_LIBPROC = []


def _darwin_split_kb(pid: int) -> tuple[int, int] | None:
    """(phys_footprint, resident size - phys_footprint) of a process in KiB, from rusage_info_v0 (sys/resource.h):
    after the 16-byte uuid, ri_resident_size is the 7th and ri_phys_footprint the 8th 64-bit field."""
    import ctypes
    if not _LIBPROC:
        _LIBPROC.append(ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True))
    info = (ctypes.c_uint64 * 12)()
    if _LIBPROC[0].proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(0), ctypes.byref(info)) != 0:
        return (0, 0) if ctypes.get_errno() == 3 else None  # ESRCH: the process is gone
    resident, footprint = info[2 + 6] // 1024, info[2 + 7] // 1024
    return footprint, max(0, resident - footprint)


def _proc_children_readable() -> bool:
    return os.path.exists(f"/proc/{os.getpid()}/task/{os.getpid()}/children")


def _darwin_children(pid: int) -> list[int] | None:
    """The child pids of a process from libproc's proc_listchildpids; None if the call fails."""
    import ctypes
    if not _LIBPROC:
        _LIBPROC.append(ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True))
    size = 256
    while True:
        buf = (ctypes.c_int * size)()
        n = _LIBPROC[0].proc_listchildpids(ctypes.c_int(pid), ctypes.byref(buf), ctypes.c_int(ctypes.sizeof(buf)))
        if n < 0:
            return None
        if n < size:
            return [buf[i] for i in range(n) if buf[i] > 0]
        size *= 4


def _darwin_tree(root: int) -> list[int] | None:
    """`root` and every process below it from proc_listchildpids; [] when `root` is gone, None if libproc fails."""
    out, seen, stack = [], set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        kids = _darwin_children(pid)
        if kids is None:
            return None
        if pid == root and not kids and _darwin_split_kb(pid) == (0, 0):
            return []
        out.append(pid)
        stack.extend(kids)
    return out


def tree_pids(root: int) -> list[int]:
    """`root` and every process below it. On Linux the tree comes from /proc/<pid>/task/<tid>/children and on macOS
    from proc_listchildpids (both cost about the size of the tree, not of the machine); elsewhere from the parent
    links of the whole process table."""
    if sys.platform == "darwin":
        found = _darwin_tree(root)
        if found is not None:
            return found
    if sys.platform.startswith("linux") and _proc_children_readable():
        out, seen, stack = [], set(), [root]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                tids = os.listdir(f"/proc/{pid}/task")
            except OSError:
                continue
            out.append(pid)
            for tid in tids:
                try:
                    with open(f"/proc/{pid}/task/{tid}/children") as f:
                        stack.extend(int(x) for x in f.read().split())
                except OSError:
                    pass
        return out
    table = process_table()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in table:
            out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _linux_alive(pid: int) -> bool:
    """False for a process that is gone or a zombie (dead, waiting for its parent to collect its status)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def tree_sample(root: int, include_root: bool = True) -> tuple[int, int, int]:
    """(memory by `memory_source()`, summed resident sets, number of live processes) of a process and all its
    descendants (without `root` itself when not `include_root`: the subreaper is organizer code, not the command),
    memory in KiB. The summed resident sets count shared file-backed pages (mapped .olean files) once per process and
    are kept for comparison only. A process whose figures cannot be read counts its whole resident set as private
    memory."""
    linux, darwin = sys.platform.startswith("linux"), sys.platform == "darwin"
    pids = [p for p in tree_pids(root) if include_root or p != root]
    split = (_linux_split_kb if linux else _darwin_split_kb if darwin else lambda pid: None)
    table = None if linux or darwin else process_table()
    private = files = rss = count = 0
    for pid in pids:
        if linux and not _linux_alive(pid):
            continue
        parts = split(pid)
        own_rss = sum(parts) if parts is not None else (table or {}).get(pid, (0, 0))[1]
        count += 1
        rss += own_rss
        own, mapped = parts or (own_rss, 0)
        private += own
        files = max(files, mapped)
    return (private + files if memory_source() != "rss_sum" else rss), rss, count


def tree_usage_kb(root: int) -> tuple[int, int]:
    """(memory by `memory_source()`, summed resident sets) of a process and all its descendants, in KiB."""
    mem, rss, _ = tree_sample(root)
    return mem, rss


def disk_usage_mb(dirs) -> float:
    """MB allocated by the files under `dirs`; symbolic links are not followed (the project's link to the pinned
    packages, the owner overlay), so only what the command wrote there counts."""
    total = 0
    for d in dirs:
        for top, _, names in os.walk(d):
            for name in names:
                try:
                    st = os.lstat(os.path.join(top, name))
                except OSError:
                    continue
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total / 2 ** 20


# Linux: a command runs below this small Python process, a child subreaper (prctl PR_SET_CHILD_SUBREAPER). A process
# of the command that double-forks or starts its own session is then reparented to it instead of to init, so it stays
# in the watched tree; when the command exits, the subreaper writes how many processes it left running to the file
# descriptor in its first argument, kills them, and exits with the command's status (or dies of the same signal).
SUBREAPER = r"""
import ctypes, os, signal, sys, time
try:
    sub = ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
except (OSError, AttributeError):
    sub = False
if not sub:
    os.execvp(sys.argv[2], sys.argv[2:])
report = int(sys.argv[1])
cmd = sys.argv[2:]
os.set_inheritable(report, False)
pid = os.fork()
if pid == 0:
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        os.write(2, ("mcb: cannot run %s: %s\n" % (cmd[0], e)).encode())
    os._exit(127)
status = 0
while True:
    try:
        got, st = os.waitpid(-1, 0)
    except ChildProcessError:
        break
    if got == pid:
        status = st
        break
me = os.getpid()
def below():
    out, stack = [], [me]
    while stack:
        p = stack.pop()
        try:
            tids = os.listdir("/proc/%d/task" % p)
        except OSError:
            continue
        for t in tids:
            try:
                kids = [int(x) for x in open("/proc/%d/task/%s/children" % (p, t)).read().split()]
            except OSError:
                kids = []
            out += kids
            stack += kids
    return out
def live(p):
    try:
        return open("/proc/%d/stat" % p).read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False
# how many processes the command left running: the watchdog counts them against max_processes
os.write(report, ("%d\n" % sum(live(p) for p in below())).encode())
os.close(report)
for _ in range(1000):
    rest = below()
    if not rest:
        break
    for p in rest:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    try:
        while os.waitpid(-1, os.WNOHANG)[0] > 0:
            pass
    except ChildProcessError:
        pass
    time.sleep(0.001)
if os.WIFSIGNALED(status):
    try:
        signal.signal(os.WTERMSIG(status), signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(me, os.WTERMSIG(status))
sys.exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)
"""


def subreaper_available() -> bool:
    return sys.platform.startswith("linux") and _proc_children_readable()


def kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL every process of a watched command's tree. With the subreaper (Linux) the processes below it are killed
    until none is left (anything orphaned meanwhile comes back to the subreaper), and the subreaper last; elsewhere
    the command's session (process group) and every process the parent links reach."""
    import signal
    root = proc.pid
    for _ in range(1000):
        rest = [p for p in tree_pids(root) if p != root
                and (not sys.platform.startswith("linux") or _linux_alive(p))]
        if not rest:
            break
        for p in rest:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.002)
    try:
        os.killpg(root, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass


class _Tail:
    """Reads a pipe to its end in a thread and keeps only its last `limit` bytes."""

    def __init__(self, pipe, limit: int):
        import collections
        import threading
        self.fd, self.limit = pipe.fileno(), limit
        self.pipe = pipe
        self.chunks: collections.deque = collections.deque()
        self.kept = self.dropped = 0
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 1 << 16)
            except OSError:
                break
            if not data:
                break
            self.chunks.append(data)
            self.kept += len(data)
            while self.kept - len(self.chunks[0]) >= self.limit:
                self.kept -= len(self.chunks[0])
                self.dropped += len(self.chunks.popleft())

    def text(self) -> str:
        data = b"".join(self.chunks)
        cut = max(0, len(data) - self.limit)
        dropped = self.dropped + cut
        head = f"[mcb: the first {dropped} bytes of output dropped]\n" if dropped else ""
        return head + data[cut:].decode("utf-8", errors="replace")

    def total_dropped(self) -> int:
        return self.dropped + max(0, self.kept - self.limit)


def _next_interval(mem_kb: int, mem_mb: int | None, procs: int, max_processes: int | None) -> float:
    gaps = [WATCH_INTERVAL_S]
    if mem_mb:
        gaps.append((mem_mb * 1024 * MEM_STOP_FRACTION - mem_kb) / 1024 / ALLOC_RATE_MB_S)
    if max_processes:
        gaps.append((max_processes - procs) / SPAWN_RATE_PER_S)
    return min(WATCH_INTERVAL_S, max(MIN_WATCH_INTERVAL_S, min(gaps)))


def _watch(proc: subprocess.Popen, cmd: list[str], timeout: float, deadline: float, mem_mb, max_processes, disk_mb,
           disk_dirs, interval: float, wrapped: bool):
    """The watchdog loop of `run`: (the limit that stopped the tree or None, (peak memory KiB, peak summed resident
    sets KiB, peak processes, peak disk MB))."""
    peak = peak_rss = peak_procs = 0
    peak_disk = 0.0
    next_disk = time.monotonic() + DISK_WATCH_INTERVAL_S
    failure = None
    while True:
        try:
            proc.wait(timeout=interval)
            break
        except subprocess.TimeoutExpired:
            pass
        mem, rss, procs = tree_sample(proc.pid, include_root=not wrapped)
        peak, peak_rss, peak_procs = max(peak, mem), max(peak_rss, rss), max(peak_procs, procs)
        now = time.monotonic()
        if mem_mb is not None and mem > mem_mb * 1024 * MEM_STOP_FRACTION:
            stop = int(mem_mb * MEM_STOP_FRACTION)
            failure = MemoryLimitExceeded(f"{mem // 1024} MB of memory ({memory_source()}) passed the stop point {stop}"
                                          f" MB ({MEM_STOP_FRACTION:.0%} of mem_mb = {mem_mb} MB)")
        elif max_processes is not None and procs > max_processes:
            failure = ProcessLimitExceeded(f"{procs} processes, more than max_processes = {max_processes}")
        elif disk_mb is not None and disk_dirs and now >= next_disk:
            used = disk_usage_mb(disk_dirs)
            peak_disk = max(peak_disk, used)
            next_disk = time.monotonic() + max(DISK_WATCH_INTERVAL_S, (time.monotonic() - now) / DISK_WALK_SHARE)
            if used >= disk_mb:
                failure = DiskLimitExceeded(f"{used:.0f} MB written to the build directories, at least disk_mb = "
                                            f"{disk_mb} MB")
        if failure is None and now > deadline:
            failure = subprocess.TimeoutExpired(cmd, timeout)
        if failure is not None:
            kill_tree(proc)
            proc.wait()
            break
        interval = _next_interval(mem, mem_mb, procs, max_processes)
    return failure, (peak, peak_rss, peak_procs, peak_disk)


def _read_report(fd: int) -> int | None:
    """The subreaper's count of processes the command left running (None if it wrote none)."""
    try:
        data = os.read(fd, 64)
    except OSError:
        data = b""
    finally:
        os.close(fd)
    return int(data) if data.strip().isdigit() else None


def _after_exit(mem_mb, max_processes, disk_mb, disk_dirs, maxrss_before: int, left: int | None, peak_procs: int,
                peak_disk: float):
    """The checks when a command has ended (whatever its exit status), for bursts the samples missed: the kernel's
    resident-set high-water mark of the finished processes, the processes it left running (counted by the subreaper
    before it kills them), and the files in the build directories. (failure or None, peak processes, peak disk MB)"""
    failure = None
    if left is not None:
        peak_procs = max(peak_procs, left + 1)
    maxrss = children_maxrss_kb()
    if disk_mb is not None and disk_dirs:
        peak_disk = max(peak_disk, disk_usage_mb(disk_dirs))
    if mem_mb is not None and maxrss > maxrss_before and maxrss > mem_mb * 1024 * MEM_STOP_FRACTION:
        failure = MemoryLimitExceeded(
            f"one process reached {maxrss // 1024} MB resident (its high-water mark, seen when it ended), past the "
            f"stop point {int(mem_mb * MEM_STOP_FRACTION)} MB ({MEM_STOP_FRACTION:.0%} of mem_mb = {mem_mb} MB)")
    elif max_processes is not None and left is not None and left + 1 > max_processes:
        failure = ProcessLimitExceeded(f"{left} processes still running when the command ended, more than "
                                       f"max_processes = {max_processes}")
    elif disk_mb is not None and disk_dirs and peak_disk >= disk_mb:
        failure = DiskLimitExceeded(f"{peak_disk:.0f} MB written to the build directories, at least disk_mb = "
                                    f"{disk_mb} MB")
    return failure, peak_procs, peak_disk


def child_rlimits(timeout: float, fsize_mb: int | None = None):
    """preexec_fn for a toolchain command: no core dumps, CPU time capped at the command's wall-clock bound times the
    number of CPUs plus a minute (each process of the command separately), so the wall-clock bound always acts first,
    and with `fsize_mb` no file written larger than that (RLIMIT_FSIZE, the one hard bound on disk: a write past it
    fails, and `run` then finds the build directory at `disk_mb`). Memory and processes are bounded by the watchdog in
    `run`, because the kernel's limits do not fit (measured on Linux): RLIMIT_AS and RLIMIT_DATA
    count Lean's address-space reservations and mapped .olean files (an `import Mathlib` process: 13.6 GB of data
    mappings for 0.45 GB of private memory; under a 1 GB RLIMIT_DATA Lean cannot start a thread), and RLIMIT_NPROC,
    even inside the sandbox's user namespace, counts every process of the user on the machine."""
    import resource
    cpu = int(timeout * (os.cpu_count() or 1)) + 60

    def apply():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if fsize_mb:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_mb << 20, fsize_mb << 20))
    return apply


def children_maxrss_kb() -> int:
    """The largest resident set (KiB) any finished, waited-for descendant of this process ever had. The kernel keeps
    this high-water mark per process and passes it up to whoever waits for it (the subreaper included), so it also
    sees a peak that came and went between two samples."""
    import resource
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return peak // 1024 if sys.platform == "darwin" else peak


def run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: float = BUILD_TIMEOUT_S,
        mem_mb: int | None = None, stats: dict | None = None, preexec_fn=None, max_processes: int | None = None,
        disk_mb: int | None = None, disk_dirs=()):
    """subprocess.run(capture_output=True, text=True). With `stats`, a limit or `preexec_fn` the command runs in its own
    session (on Linux below the SUBREAPER, so orphans stay in its tree) under a watchdog that samples the tree: memory
    (`tree_sample`, counted by `memory_source()`), the number of processes, and every DISK_WATCH_INTERVAL_S the files
    in `disk_dirs`. Peaks go to stats (peak_mem_kb, peak_tree_rss_kb, peak_processes, peak_disk_mb). Memory past
    MEM_STOP_FRACTION of `mem_mb` (MemoryLimitExceeded), more than `max_processes` processes (ProcessLimitExceeded) or
    at least `disk_mb` MB in `disk_dirs` (DiskLimitExceeded) kills the whole tree; these are InfrastructureStops. The
    wall-clock `timeout` also kills the tree (subprocess.TimeoutExpired). Only the last OUTPUT_TAIL_BYTES of stdout
    and of stderr are kept (stats["output_dropped_bytes"] counts the rest); bytes that are not UTF-8 are replaced."""
    if stats is None and mem_mb is None and preexec_fn is None and max_processes is None and disk_mb is None:
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if shutil.which(cmd[0], path=(env or os.environ).get("PATH")) is None and not os.access(cmd[0], os.X_OK):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])
    wrapped = subreaper_available()
    report_r, report_w = os.pipe() if wrapped else (None, None)
    argv = [sys.executable, "-I", "-c", SUBREAPER, str(report_w), *cmd] if wrapped else cmd
    maxrss_before = children_maxrss_kb()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=preexec_fn, pass_fds=(report_w,) if wrapped else ())
    finally:
        if wrapped:
            os.close(report_w)
    out, err = _Tail(proc.stdout, OUTPUT_TAIL_BYTES), _Tail(proc.stderr, OUTPUT_TAIL_BYTES)
    deadline = time.monotonic() + timeout
    interval = _next_interval(0, mem_mb, 1, max_processes)
    failure = None
    try:
        failure, peaks = _watch(proc, cmd, timeout, deadline, mem_mb, max_processes, disk_mb, disk_dirs, interval,
                                wrapped)
    except BaseException:
        # interrupted (Ctrl-C) or failed: the command's tree must not outlive this process, and the killed command is
        # reaped and its pipes closed
        kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for tail in (out, err):
            tail.thread.join(timeout=30)
            if not tail.thread.is_alive():
                tail.pipe.close()
        if wrapped:
            os.close(report_r)
        raise
    peak, peak_rss, peak_procs, peak_disk = peaks
    left = _read_report(report_r) if wrapped else None
    if failure is None:
        failure, peak_procs, peak_disk = _after_exit(mem_mb, max_processes, disk_mb, disk_dirs, maxrss_before,
                                                     left, peak_procs, peak_disk)
    if failure is None and not wrapped:
        # whatever the command left running in its session holds the pipes open
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    for tail in (out, err):
        tail.thread.join(timeout=30)
        if not tail.thread.is_alive():
            tail.pipe.close()
    if stats is not None:
        stats["peak_mem_kb"] = max(stats.get("peak_mem_kb", 0), peak)
        stats["peak_tree_rss_kb"] = max(stats.get("peak_tree_rss_kb", 0), peak_rss)
        stats["memory_source"] = memory_source()
        stats["peak_processes"] = max(stats.get("peak_processes", 0), peak_procs)
        if disk_dirs:
            stats["peak_disk_mb"] = round(max(stats.get("peak_disk_mb", 0), peak_disk), 1)
        stats["output_dropped_bytes"] = stats.get("output_dropped_bytes", 0) + out.total_dropped() \
            + err.total_dropped()
    if failure is not None:
        raise failure
    return subprocess.CompletedProcess(cmd, proc.returncode, out.text(), err.text())


def lean_env() -> dict:
    env = dict(os.environ)
    for key in ("LEAN_PATH", "LEAN_SRC_PATH", "LAKE_HOME"):
        env.pop(key, None)
    return env


# Variables a toolchain subprocess may inherit; every LAKE_*, LEAN_* and ELAN_* variable is dropped.
ENV_KEEP = ("HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "LD_LIBRARY_PATH")


def clean_env(toolchain: Path) -> dict:
    """Minimal environment for Lean/Lake: the pinned toolchain first on PATH, no Lean/Lake/elan overrides."""
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    # Lake runs git on the pinned packages; keep the caller's git (on some Macs /usr/bin/git is a broken Xcode shim,
    # and a failing `git remote get-url` makes Lake delete and re-clone the package)
    git = shutil.which("git")
    git_dir = [str(Path(git).parent)] if git else []
    env["PATH"] = ":".join(dict.fromkeys([str(toolchain / "bin"), *git_dir, "/usr/bin", "/bin"]))
    return env


def resolve_toolchain(task: dict, cwd: Path) -> tuple[Path | None, list[str]]:
    """The toolchain root: LCBENCH_TOOLCHAIN, else the prefix reported by the `lean` on PATH (an elan proxy picks
    the project's pinned toolchain). Every tool is then called by absolute path and must be a real Lean binary."""
    want = task["toolchain"]["lean_githash"]
    root_env = os.environ.get("LCBENCH_TOOLCHAIN")
    if root_env:
        root = Path(root_env)
    else:
        try:
            out = run(["lean", "--print-prefix"], cwd, lean_env(), timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None, [f"toolchain mismatch: no `lean` on PATH; install {task['toolchain']['id']} "
                          "(elan) or set LCBENCH_TOOLCHAIN to its directory"]
        root = Path(out.stdout.strip() or "/nonexistent")
    root = root.resolve()
    for tool in ("lean", "lake", "leanchecker"):
        exe = root / "bin" / tool
        if not exe.is_file():
            return None, [f"toolchain not trusted: {exe} is missing (toolchain root {root})"]
        with open(exe, "rb") as f:
            if f.read(2) == b"#!":
                return None, [f"toolchain not trusted: {exe} is a script, not a Lean binary"]
    try:
        got = run([str(root / "bin/lean"), "--githash"], cwd, clean_env(root), timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        got = f"({e})"
    if got != want:
        return None, [f"toolchain mismatch: {root}/bin/lean --githash is {got!r}, task pins {want!r} "
                      f"({task['toolchain']['id']}); install that toolchain (elan) or set LCBENCH_TOOLCHAIN"]
    return root, []


def toolchain_problems(task: dict, cwd: Path) -> list[str]:
    return resolve_toolchain(task, cwd)[1]


def module_path(module: str) -> str:
    return module.replace(".", "/")


def package_problems(task: dict, packages_dir: Path | None) -> list[str]:
    """The dependency closure must hold every pinned Lake package at its pinned git revision."""
    pins = task["project"].get("packages", [])
    if not pins:
        return []
    if packages_dir is None or not packages_dir.is_dir():
        return ["task needs its pinned Lake packages: run start with --deps DIR or with network for lake"]
    problems = []
    for pin in pins:
        pdir = packages_dir / pin["name"]
        got = run(["git", "rev-parse", "HEAD"], pdir, timeout=60).stdout.strip() if pdir.is_dir() else "(missing)"
        if got != pin["rev"]:
            problems.append(f"package {pin['name']} is at {got}, task pins {pin['rev']}")
            continue
        url = run(["git", "remote", "get-url", "origin"], pdir, timeout=60).stdout.strip()
        if url != pin["url"]:
            problems.append(f"package {pin['name']}: remote URL {url!r} differs from the pinned {pin['url']!r}; "
                            "refusing to build (lake would delete and re-clone it)")
    return problems


def stage_project(task: dict, fixed_root: Path, editable_root: Path, dest: Path,
                  packages_dir: Path | None = None) -> list[str]:
    """Fresh project = task's fixed files (hash-checked) + the editable sources + a link to the
    pinned dependency closure (never copied, may be read-only); returns problems."""
    problems = package_problems(task, packages_dir)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel, digest in task["project"]["fixed_files"].items():
        src = fixed_root / rel
        if not src.is_file() or sha256_file(src) != digest:
            problems.append(f"fixed project file changed or missing: {rel}")
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
    for rel in task["project"]["editable"]:
        src = editable_root / rel
        if not src.is_file():
            problems.append(f"editable source missing: {rel}")
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
    if task["project"].get("packages") and packages_dir is not None:
        (dest / ".lake").mkdir(exist_ok=True)
        (dest / ".lake/packages").symlink_to(packages_dir.resolve())
    return problems


# Lean 4.32 writes linter warnings, with the absolute path of the source file, into the .olean (lintLogExt), so
# every build of a scored project happens at one canonical path: <build root>/<task sha256[:12]>/project.
CANONICAL_BUILD_ROOT = Path("/private/tmp/lcbench-build")


HOST_TMP = (Path("/tmp"), Path("/var/tmp"))


def default_build_root(env: dict, has_canonical: bool, home: Path) -> Path:
    """LCBENCH_BUILD_ROOT, else the canonical root when the host has it (macOS, where /tmp links into /private), else
    `lcbench-build` in TMPDIR when that is a private directory (a cluster job's node scratch), else in the user's cache
    directory. On Linux the host's /tmp and /var/tmp are never used. The Linux sandbox mounts the canonical root."""
    if env.get("LCBENCH_BUILD_ROOT"):
        return Path(env["LCBENCH_BUILD_ROOT"])
    if has_canonical:
        return CANONICAL_BUILD_ROOT
    tmpdir = env.get("TMPDIR")
    if tmpdir and not any(Path(tmpdir) == t or t in Path(tmpdir).parents for t in HOST_TMP):
        return Path(tmpdir) / "lcbench-build"
    return Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "lcbench-build"


def build_root() -> Path:
    """Where builds happen without a sandbox (default_build_root for this process)."""
    return default_build_root(os.environ, CANONICAL_BUILD_ROOT.parent.is_dir(), Path.home())


# Wall-clock safety cap of one evaluation (seconds): reaching it is an infrastructure timeout (the machine was too slow
# or overloaded; re-run on another machine), never an invalid submission.
SAFETY_WALL_S = 6 * 3600
# Default per-task limits; TASK.json `limits` overrides them and check and verify apply the same values. The only
# validity limit is machine-independent: kernel heartbeats per declaration (Lean's deterministic count of small
# allocations while the kernel checks it; 0 = none). The others are safety limits that protect the machine and never
# decide validity: the wall-clock cap of the whole evaluation, memory (mem_mb, None = not watched), processes in a
# command's tree, and MB written to the build directory.
DEFAULT_LIMITS = {"kernel_heartbeats": 0, "mem_mb": None, "safety_wall_s": SAFETY_WALL_S, "max_processes": 512,
                  "disk_mb": 16384}


LIMIT_UNITS = {"kernel_heartbeats": "heartbeats per declaration", "mem_mb": "MB", "safety_wall_s": "s",
               "max_processes": "processes", "disk_mb": "MB"}
INFRASTRUCTURE_TIMEOUT = "infrastructure timeout"
INFRASTRUCTURE_STOP = "infrastructure stop"
INFRASTRUCTURE_ERROR = "infrastructure error"


def task_limits(task: dict) -> dict:
    return {**DEFAULT_LIMITS, **task.get("limits", {})}


class LocalRunner:
    """Runs the pinned toolchain directly (./check, and `verify --sandbox none`) in the canonical project directory.
    A lock file serializes concurrent evaluations of the same task on one machine."""
    sandbox = "none"

    mem_mb: int | None = None
    max_processes: int | None = None
    disk_mb: int | None = None

    def __init__(self, toolchain: Path, task_sha: str, root: Path | None = None):
        self.toolchain = toolchain
        self.base = (root or build_root()) / task_sha[:12]
        self.project = self.base / "project"
        self.inside_project = self.project
        self._lock = None
        self.stats: dict = {}

    def __enter__(self):
        import fcntl
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = open(self.base.with_name(self.base.name + ".lock"), "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.project.exists():
            shutil.rmtree(self.project, ignore_errors=True)
        # the empty <sha12> directory goes too (a waiting process re-creates it); the small lock file stays, because
        # removing a lock file that another process may be waiting on would let a third one in
        try:
            self.base.rmdir()
        except OSError:
            pass
        self._lock.close()

    def tool(self, name: str) -> str:
        return str(self.toolchain / "bin" / name)

    def expose(self, path: Path) -> None:
        """Make an organizer directory (the compiled metric tool) readable by the commands this runner starts."""

    def run(self, cmd: list[str], timeout: float, extra_env: dict | None = None):
        return run(cmd, self.project, {**clean_env(self.toolchain), **(extra_env or {})}, timeout,
                   self.mem_mb, self.stats, child_rlimits(timeout, self.disk_mb), self.max_processes, self.disk_mb,
                   [self.project])


class DirectRunner(LocalRunner):
    """Runs the toolchain in a given project directory (organizer make-task; the .olean bytes are not used)."""

    def __init__(self, toolchain: Path, project: Path):
        self.toolchain = toolchain
        self.base = project.parent
        self.project = self.inside_project = project
        self.stats = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


# Every background module a build may load is pinned: the package ships one line "<digest>  <module>" per module
# (the digest hashes the module's .olean* files), and TASK.json pins the sha256 of that file.
BACKGROUND_MANIFEST = ".benchmark/background.sha256"


def lake_lean_path(runner) -> str:
    """The project's `lake env` LEAN_PATH as the runner's processes see it (paths inside a sandbox)."""
    return runner.run([runner.tool("lake"), "env", "printenv", "LEAN_PATH"], 600).stdout.strip()


def lean_path_dirs(runner) -> list[Path]:
    """The project's `lake env` LEAN_PATH (toolchain included) as host paths: a sandbox shows the project directory
    at `inside_project`, and the pinned packages are reached through the project's .lake/packages link."""
    inside, dirs = str(runner.inside_project), []
    for d in lake_lean_path(runner).split(":"):
        if d == inside or d.startswith(inside + "/"):
            d = str(runner.project) + d[len(inside):]
        if d:
            dirs.append(Path(d))
    return dirs


# An editable module may live inside a pinned dependency package: the Mathlib-leaf tasks edit leaf modules of the
# Mathlib package, which the project rebuilds from the submitted bytes. `lake env` lists the dependencies' lib
# directories before the project's own, and Lean resolves a module in the first LEAN_PATH entry that holds its
# top-level directory (`Mathlib/`), so the dependency's original .olean would be loaded instead of the rebuilt one.
# The organizer stages then run with an overlay first on LEAN_PATH: a mirror of the dependency's directory for that
# top-level name, made of symlinks, in which only the charged modules point at the project's build.
OWNER_OVERLAY = ".lake/lcbench-owner-overlay"


def link_target(target: Path, link_dir: Path, project: Path) -> str:
    """A symlink target that resolves identically outside and inside the sandbox: relative within the project (which
    the sandbox shows at another path), absolute elsewhere (the sandbox mounts those paths at their host paths)."""
    if target == project or project in target.parents:
        return os.path.relpath(target, link_dir)
    return str(target)


def merge_tree(dep: Path, own: Path, dest: Path, modules: list[list[str]], project: Path) -> None:
    """dest mirrors the dependency directory `dep` with symlinks, except that each module in `modules` (its name
    components below `dep`) links to its files in the project's build directory `own`."""
    dest.mkdir(parents=True)
    stems = {m[0] for m in modules if len(m) == 1}
    below: dict[str, list[list[str]]] = {}
    for m in modules:
        if len(m) > 1:
            below.setdefault(m[0], []).append(m[1:])
    for entry in sorted(dep.iterdir()) if dep.is_dir() else []:
        if entry.name in below and entry.is_dir():
            continue
        if not entry.is_dir() and entry.name.split(".", 1)[0] in stems:
            continue
        (dest / entry.name).symlink_to(link_target(entry, dest, project))
    for name, sub in sorted(below.items()):
        merge_tree(dep / name, own / name, dest / name, sub, project)
    for entry in sorted(own.iterdir()) if own.is_dir() else []:
        if entry.is_file() and entry.name.split(".", 1)[0] in stems:
            (dest / entry.name).symlink_to(link_target(entry, dest, project))


def owner_lean_path(runner, owners: list[str]) -> str | None:
    """None when every charged module's top-level name resolves to the project's own build first (the usual case);
    otherwise the LEAN_PATH, as the runner's processes see it, with a fresh owner overlay in front. Directories are
    compared resolved: `lake env` prints resolved paths, and the project may be reached through a symbolic link
    (for example a build root on a node scratch disk reached through a symbolic link)."""
    project = runner.project
    own = project / ".lake/build/lib/lean"
    overlay = project / OWNER_OVERLAY
    if overlay.is_symlink() or overlay.is_file():
        overlay.unlink()
    elif overlay.exists():
        shutil.rmtree(overlay)
    lean_path = lake_lean_path(runner)
    dirs = lean_path_dirs(runner)
    shadowed = False
    for top in sorted({m.split(".")[0] for m in owners}):
        first = next((d for d in dirs if (d / top).is_dir() or (d / f"{top}.olean").is_file()), None)
        if first is None or first.resolve() == own.resolve():
            continue
        if top in owners:
            raise RuntimeError(f"charged module {top} is shadowed by {first} and has no parent directory to overlay")
        merge_tree(first / top, own / top, overlay / top,
                   [m.split(".")[1:] for m in owners if m.split(".")[0] == top], project)
        shadowed = True
    return f"{runner.inside_project / OWNER_OVERLAY}:{lean_path}" if shadowed else None


def toolchain_modules(toolchain: Path) -> list[str]:
    """Every module the toolchain ships (Init, Std, Lean, Lake), from its lib/lean/**/*.olean files."""
    lib = toolchain / "lib/lean"
    return sorted(p.relative_to(lib).with_suffix("").as_posix().replace("/", ".") for p in lib.rglob("*.olean"))


PINNED_OLEAN_SUFFIXES = (".olean", ".olean.server")


def background_module_digests(task: dict, dirs: list[Path], project: Path, modules) -> dict[str, str]:
    """{module: digest} for every background module among `modules`, where the digest is the sha256 of the lines
    "<sha256>  <file>" of the module's .olean and .olean.server files, taken from the first LEAN_PATH directory that
    has them (the project's own build output excluded); a module with no .olean is "missing". Files are hashed in
    parallel. The .olean.private part is left out: its bytes differ between Mathlib builds of the same commit (10 of
    about 8,000 modules differed between the cache fetched on 2026-09-18 and on 2026-09-30), so pinning it would
    reject participants whose Mathlib cache differs from the organizer's."""
    from concurrent.futures import ThreadPoolExecutor
    own = (project / ".lake/build").resolve()
    search = [d for d in dirs if not d.resolve().is_relative_to(own)]
    found: dict[str, list[Path]] = {}
    for m in sorted(set(modules)):
        if not task_in_background(task, m):
            continue
        found[m] = []
        for d in search:
            files = sorted(p for p in d.glob(module_path(m) + ".olean*") if p.name.endswith(PINNED_OLEAN_SUFFIXES))
            if files:
                found[m] = files
                break
    every = [p for files in found.values() for p in files]
    with ThreadPoolExecutor(8) as pool:
        digests = dict(zip(every, pool.map(sha256_file, every)))
    return {m: sha256_bytes("".join(f"{digests[p]}  {p.name}\n" for p in files).encode()) if files else "missing"
            for m, files in found.items()}


def background_manifest_bytes(digests: dict[str, str]) -> bytes:
    return "".join(f"{d}  {m}\n" for m, d in sorted(digests.items())).encode()


def read_background_manifest(data: bytes) -> dict[str, str]:
    return {m: d for d, _, m in (line.partition("  ") for line in data.decode().splitlines())}


def background_problems(task: dict, runner, manifest: Path | None) -> list[str]:
    """Re-hash every pinned background module after the build: the manifest must be the one TASK.json pins, and each
    module's .olean files must hash to its pinned digest."""
    pinned = task["background"]["olean_sha256_root"]
    if manifest is None or not manifest.is_file():
        return [f"background hash mismatch: the background manifest {BACKGROUND_MANIFEST} is missing"]
    data = manifest.read_bytes()
    if sha256_bytes(data) != pinned:
        return [f"background hash mismatch: {BACKGROUND_MANIFEST} hashes to {sha256_bytes(data)}, "
                f"TASK.json pins {pinned}"]
    want = read_background_manifest(data)
    got = background_module_digests(task, lean_path_dirs(runner), runner.project, want)
    bad = sorted(m for m in want if got.get(m) != want[m])
    if not bad:
        return []
    return [f"background hash mismatch: {len(bad)} background module(s) differ from their pinned .olean hashes: "
            + ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")]


# The organizer Lean tools (lean/ in the lcbench repo, .benchmark/tools/ in a package): the metric counter
# lcbench-metric and lcbench-check (kernel replay; validity facts and metrics from one import). Compiled once per
# (tool sources, toolchain) outside every participant build, then run on the built project with `lake env`.
METRIC_TOOL_FILES = ("lakefile.toml", "lake-manifest.json", "lean-toolchain", "Main.lean", "Check.lean",
                     "LcbenchTools/Metric.lean", "LcbenchTools/Request.lean",
                     "LcbenchTools/Replay.lean", "LcbenchTools/BudgetReplay.lean", "LcbenchTools/Extract.lean")
TOOL_EXES = ("lcbench-metric", "lcbench-check")
METRIC_TOOL_BUILD_S = 3600


def metric_tool(tools_dir: Path, toolchain: Path, exe_name: str = "lcbench-metric") -> Path:
    """Compile the organizer tools (TOOL_EXES) from `tools_dir` with the pinned toolchain under <build root>/tools/<key>,
    where the key hashes the tool sources and the toolchain; returns the executable `exe_name`. Raises RuntimeError
    if the build fails."""
    import fcntl
    listing = "".join(f"{sha256_file(tools_dir / rel)}  {rel}\n" for rel in METRIC_TOOL_FILES)
    key = sha256_bytes(f"{listing}{toolchain.resolve()}\n".encode())[:16]
    # resolved: the sandbox mounts this directory at its real path, so a symlinked build root (a link into a node's
    # scratch disk, say) must not appear in the command
    (build_root() / "tools").mkdir(parents=True, exist_ok=True)
    base = (build_root() / "tools").resolve() / key
    exes = [base / ".lake/build/bin" / name for name in TOOL_EXES]
    exe = base / ".lake/build/bin" / exe_name
    with open(base.with_name(key + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if all(e.is_file() for e in exes):
            return exe
        if base.exists():
            shutil.rmtree(base)
        for rel in METRIC_TOOL_FILES:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tools_dir / rel, base / rel)
        built = run([str(toolchain / "bin/lake"), "build", *TOOL_EXES], base, clean_env(toolchain),
                    METRIC_TOOL_BUILD_S)
        if built.returncode != 0 or not all(e.is_file() for e in exes):
            raise RuntimeError(f"building {', '.join(TOOL_EXES)} failed:\n{(built.stdout + built.stderr)[-3000:]}")
    return exe


def background_request(task: dict) -> dict:
    """The background rule in the metric counter's request format: prefixes plus explicitly listed modules."""
    bg = task["background"]
    return {"prefixes": bg["modules"],
            "modules": sorted(set(bg.get("explicit_modules", [])) | set(bg.get("fixed_modules", [])))}


def paranoid_default() -> bool:
    """Whether stock `leanchecker` also replays every owner module when the caller does not say (LCBENCH_PARANOID=1)."""
    return os.environ.get("LCBENCH_PARANOID") == "1"


def build_and_extract(task: dict, project: Path, tools_dir: Path, runner=None,
                      background_manifest: Path | None = None, paranoid: bool = False, replay: bool = True) -> dict:
    """`build_stages`; an organizer helper command that overruns its own wall-clock bound (`lake env printenv`,
    compiling the organizer tools) is an infrastructure timeout too, never a verdict."""
    try:
        return build_stages(task, project, tools_dir, runner, background_manifest, paranoid, replay)
    except subprocess.TimeoutExpired as e:
        cmd = " ".join(str(c) for c in (e.cmd or [])[:3])
        return {"stage": "infrastructure", "timings": {},
                "log": f"{INFRASTRUCTURE_TIMEOUT}: the organizer step `{cmd}` did not finish within {e.timeout} s "
                       f"(the machine is too slow or overloaded); this says nothing about the submission: re-run it "
                       f"on another machine"}
    except InfrastructureStop as e:
        return {"stage": "infrastructure", "timings": {}, "log": stop_message(e, "an organizer step")}


# What each timed stage is called in a no-verdict reason (its key in `timings` names the stage, not a limit).
STAGE_NAMES = {"build_s": "the build (lake build)", "kernel_replay_s": "the kernel replay",
               "kernel_replay_leanchecker_s": "the stock leanchecker cross-check",
               "export_s": "the export (validity facts and metrics)"}


def stop_message(stop: InfrastructureStop, during: str) -> str:
    """The NO VERDICT reason of a safety stop (memory, processes, disk)."""
    return (f"{INFRASTRUCTURE_STOP}: the safety limit {stop.limit} stopped {during}: {stop}; this protects the machine "
            f"and says nothing about the submission: re-run it on another machine (one with more memory or disk if "
            f"it stops again)")


def build_stages(task: dict, project: Path, tools_dir: Path, runner=None,
                 background_manifest: Path | None = None, paranoid: bool = False, replay: bool = True) -> dict:
    """Build, re-hash the pinned background .olean files (when TASK.json pins them), replay every owner module into
    the kernel (`lcbench-check replay`: stock leanchecker's check with one background import), then read the validity
    facts (statements, axioms, frozen closures, imports) and count and export the charged declarations
    (`lcbench-check facts`: the extractor and the metric counter on one import of the owner modules). The organizer
    tools are compiled executables that import the owner modules at run time in a fresh process (no participant
    initializers, no organizer command elaborated in the participant's environment). With `paranoid`, stock
    `leanchecker` also replays every owner module (one process each), and the two replays must agree. `replay=False`
    is only for make-task's second extraction of the same build (the first one replayed it); check and verify always
    replay."""
    if runner is None:
        # make-task passes a draft task without `toolchain`; it checks `lean --githash` itself
        pinned = task if "toolchain" in task else {"toolchain": {"id": "(from PATH)", "lean_githash": run(
            ["lean", "--githash"], project, lean_env(), timeout=60).stdout.strip()}}
        toolchain, problems = resolve_toolchain(pinned, project)
        if problems:
            return {"stage": "toolchain", "log": "\n".join(problems), "timings": {}}
        runner = DirectRunner(toolchain, project)
    limits = task_limits(task)
    timings = {}
    # one wall-clock deadline for the whole evaluation; it never decides validity (see `infrastructure`)
    deadline = time.monotonic() + limits["safety_wall_s"]

    exceeded = {}

    def timed(stage: str, cmd: list[str], extra_env=None):
        started = time.monotonic()
        try:
            out = runner.run(cmd, max(1.0, deadline - started), extra_env)
        except subprocess.TimeoutExpired:
            exceeded["infrastructure"] = stage
            timings[stage] = round(timings.get(stage, 0) + time.monotonic() - started, 1)
            return None
        except InfrastructureStop as e:
            exceeded["stop"] = (stage, e)
            timings[stage] = round(timings.get(stage, 0) + time.monotonic() - started, 1)
            return None
        timings[stage] = round(timings.get(stage, 0) + time.monotonic() - started, 1)
        return out

    def stopped() -> dict:
        """A command was stopped by a safety limit (wall-clock cap, memory, processes, disk): never a verdict."""
        if "infrastructure" in exceeded:
            return {"stage": "infrastructure", "timings": timings,
                    "log": f"{INFRASTRUCTURE_TIMEOUT}: the wall-clock safety cap safety_wall_s = "
                           f"{limits['safety_wall_s']} s was reached during "
                           f"{STAGE_NAMES.get(exceeded['infrastructure'], exceeded['infrastructure'])} (the machine "
                           f"is too slow or overloaded); this says nothing about the submission: re-run it on another "
                           f"machine"}
        stage, stop = exceeded["stop"]
        return {"stage": "infrastructure", "timings": timings,
                "log": stop_message(stop, STAGE_NAMES.get(stage, stage))}

    built = timed("build_s", [runner.tool("lake"), "build"])
    if built is None:
        return stopped()
    if built.returncode != 0:
        return {"stage": "build", "log": (built.stdout + built.stderr)[-6000:], "timings": timings}
    pinned = task["background"].get("olean_sha256_root")
    if pinned:
        started = time.monotonic()
        problems = background_problems(task, runner, background_manifest)
        timings["background_s"] = round(time.monotonic() - started, 1)
        if problems:
            return {"stage": "background", "problems": problems, "log": "\n".join(problems), "timings": timings}
    owners = task["project"]["owner_modules"]
    try:
        tool_lean_path = owner_lean_path(runner, owners)
    except RuntimeError as err:
        return {"stage": "build", "log": str(err), "timings": timings}

    def lake_env(cmd: list[str]) -> list[str]:
        """`lake env` for an organizer stage; with the owner overlay in front of LEAN_PATH when one is needed."""
        lean_path = ["/usr/bin/env", f"LEAN_PATH={tool_lean_path}"] if tool_lean_path else []
        return [runner.tool("lake"), "env", *lean_path, *cmd]

    try:
        check_exe = metric_tool(tools_dir, runner.toolchain, "lcbench-check")
    except RuntimeError as err:
        # the organizer's own tools (the verifier's sources, not the submission's) did not build on this machine
        return {"stage": "infrastructure", "timings": timings,
                "log": f"{INFRASTRUCTURE_ERROR}: the organizer tools could not be built on this machine; this says "
                       f"nothing about the submission: re-run it on another machine\n{err}"}
    runner.expose(check_exe.parents[3])
    if not replay:
        return facts_stage(task, runner, check_exe, lake_env, timed, stopped, owners, pinned, background_manifest,
                           [], {}, {"mode": "skipped", "fallback_reason": "make-task re-extraction of a replayed build"},
                           timings)
    # one process replays every owner, each declaration in the kernel with the task's heartbeat budget
    budget = limits["kernel_heartbeats"]
    replay = timed("kernel_replay_s", lake_env([str(check_exe), "replay", "--kernel-heartbeats", str(budget),
                                                "--jobs", str(replay_jobs()), *owners]))
    if replay is None:
        return stopped()
    try:
        report = json.loads(replay.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        report = {"ok": False, "error": f"no replay report (exit {replay.returncode})", "failed_module": None}
    heartbeats = replay_heartbeats(report, budget)
    # which replay ran: one shared background import, or stock leanchecker's check per owner (and why)
    mode = {"mode": report.get("mode"), "fallback_reason": report.get("fallback_reason") or None}
    if report.get("over_budget"):
        return {"stage": "limit", "limit": "kernel_heartbeats", "declaration": report["over_budget"],
                "heartbeats": heartbeats, "kernel_replay_mode": mode, "timings": timings,
                "log": f"limit: kernel_heartbeats = {budget} exceeded by {report['over_budget']}"}
    if replay.returncode != 0 or not report.get("ok"):
        log = f"kernel replay found a problem in {report.get('failed_module')}\n{report.get('error')}\n{replay.stderr}"
        return {"stage": "kernel_replay", "log": log[-6000:], "timings": timings, "heartbeats": heartbeats,
                "kernel_replay_mode": mode}
    replay_logs = [f"kernel replay {row['module']}: ok ({report['mode']})" for row in report["modules"]]
    if paranoid:
        for m in owners:
            stock = timed("kernel_replay_leanchecker_s", lake_env([runner.tool("leanchecker"), m]))
            if stock is None:
                return {**stopped(), "kernel_replay_mode": mode}
            if stock.returncode != 0:
                log = f"stock leanchecker rejects {m}, lcbench-check replay accepted it (cross-check disagreement)\n"
                return {"stage": "kernel_replay", "log": (log + stock.stdout + stock.stderr)[-6000:],
                        "timings": timings, "kernel_replay_mode": mode}
            replay_logs.append(f"leanchecker {m}: ok")
    return facts_stage(task, runner, check_exe, lake_env, timed, stopped, owners, pinned, background_manifest,
                       replay_logs, heartbeats, mode, timings)


def facts_stage(task: dict, runner, check_exe: Path, lake_env, timed, stopped, owners: list[str], pinned,
                background_manifest: Path | None, replay_logs: list[str], heartbeats: dict, mode: dict,
                timings: dict) -> dict:
    """The validity facts and the metrics of a built, replayed project (`lcbench-check facts`): the last stage of
    build_stages."""
    out = runner.project / "bench_extract__.json"
    if out.exists():
        out.unlink()
    with_export = kernel_export_kept(task)
    export_name = "bench_metric_export__.ndjson"
    # the extractor gives only validity facts (no charged closure, no node counts, no export): the counter scores
    request = {"modules": owners, "targets": [t["name"] for t in task["targets"]],
               "background": background_request(task), "per_declaration": True,
               "extract": {"roots": [t["name"] for t in task["targets"]], "frozen": [f["name"] for f in task["frozen"]],
                           "out": str(runner.inside_project / out.name)}}
    if with_export:
        request["export"] = str(runner.inside_project / export_name)
    # the counter's result is one JSON line that grows with the number of declarations (tens of MB for a large task),
    # longer than the OUTPUT_TAIL_BYTES of stdout that `run` keeps: it goes to a file, and the output only to the log
    result_file = runner.project / "bench_metric_result__.json"
    if result_file.exists():
        result_file.unlink()
    request["result"] = str(runner.inside_project / result_file.name)
    (runner.project / "bench_metric_request__.json").write_text(json.dumps(request, sort_keys=True))
    request_path = str(runner.inside_project / "bench_metric_request__.json")
    counted = timed("export_s", lake_env([str(check_exe), "facts", request_path]))

    def unpinned_problems(data: dict) -> list[str]:
        """Every background module the owners import must be one of the pinned ones checked above."""
        if not pinned:
            return []
        want = read_background_manifest(background_manifest.read_bytes())
        unpinned = sorted(m for m in data["owner_import_closure"] if task_in_background(task, m) and m not in want)
        return [f"background module not pinned: {m} (not in {BACKGROUND_MANIFEST})" for m in unpinned[:5]]

    if counted is None:
        # the facts were complete before the limit hit: the pin rule is checked first, as when they had their own step
        problems = unpinned_problems(json.loads(out.read_text())) if out.exists() else []
        if problems:
            return {"stage": "background", "problems": problems, "log": "\n".join(problems), "timings": timings,
                    "kernel_replay_mode": mode}
        return {**stopped(), "kernel_replay_mode": mode}
    if not out.exists():
        return {"stage": "extract", "log": (counted.stdout + counted.stderr)[-6000:], "timings": timings,
                "kernel_replay_mode": mode}
    data = json.loads(out.read_text())
    problems = unpinned_problems(data)
    if problems:
        return {"stage": "background", "problems": problems, "log": "\n".join(problems), "timings": timings,
                "kernel_replay_mode": mode}
    metric = read_metric_result(result_file, counted.returncode)
    if counted.returncode != 0 or "error" in metric:
        # the extractor's facts still name the rule a submission breaks (sorry, extra axioms, statements)
        return {"stage": "metric", "data": data, "timings": timings, "kernel_replay_mode": mode,
                "log": f"{metric.get('error')}\n{counted.stderr}"[-6000:]}
    data["total"] = metric["metrics"]["expr_nodes_with_levels_v1"]
    # the counter's own times of its passes (machine-dependent, recorded only): the rest of export_s is the process
    # start, the extractor and writing the result
    for key, ms in (metric.get("ms") or {}).items():
        if key in EXPORT_PASS_TIMINGS and isinstance(ms, int):
            timings[EXPORT_PASS_TIMINGS[key]] = round(ms / 1000, 1)
    for metric_id in RECORDED_METRICS:
        data[metric_id] = metric["metrics"][metric_id]
    data["declarations"] = [{"name": r["name"], "kind": r["kind"], "role": r["role"],
                             "cost": r["expr_nodes_with_levels_v1"]} for r in metric["declarations"]]
    data["export"] = (runner.project / export_name).read_bytes() if with_export else None
    return {"stage": "ok", "data": data, "replay": replay_logs, "timings": timings,
            "heartbeats": heartbeats, "kernel_replay_mode": mode}


def replay_jobs() -> int:
    """How many owner modules the kernel replay checks at once when it runs one process per owner: the CPUs this
    process may use (a batch job's or a sandbox's allocation, not the whole machine), at most 16. The verdict and
    the report rows do not depend on it, only the time."""
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    return max(1, min(16, cpus))


def read_metric_result(path: Path, returncode: int) -> dict:
    """The counter's lcbench.metric_result.v1 from the request's "result" file (`lcbench-check facts`), read whole;
    {"error": "no result (exit N)"} when the counter wrote no complete result there."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"error": f"no result (exit {returncode})"}


# the counter's pass timings ("ms" of lcbench.metric_result.v1, Metric.measure) -> timings keys (seconds)
EXPORT_PASS_TIMINGS = {"import": "export_import_s", "closure": "export_closure_s",
                       "expr_nodes_with_levels_v1": "export_tree_counts_s", "export_and_dag": "export_write_and_dag_s"}


def replay_heartbeats(report: dict, budget: int) -> dict:
    """The kernel heartbeats of a replay report (lcbench-check replay): total over every checked declaration and the
    largest single declaration, which the budget applies to. Deterministic: they do not depend on the machine."""
    rows = report.get("modules") or []
    top = max(rows, key=lambda r: r.get("max_declaration_heartbeats", 0), default={})
    return {"kernel_budget_per_declaration": budget,
            "kernel_total": sum(r.get("heartbeats", 0) for r in rows),
            "kernel_declarations": sum(r.get("declarations_checked", 0) for r in rows),
            "kernel_max_declaration_heartbeats": top.get("max_declaration_heartbeats", 0),
            "kernel_max_declaration": top.get("max_declaration")}


# ---------------------------------------------------------------- task checks and metric

def statement_sha256(ser: str) -> str:
    return sha256_bytes(ser.encode())


def frozen_closure_sha256(row: dict) -> str:
    return sha256_bytes("\n".join(sorted(set(row["closure_sers"]))).encode())


def in_background(module: str, background: list[str]) -> bool:
    return any(module == b or module.startswith(b + ".") for b in background)


def task_in_background(task: dict, module: str) -> bool:
    """The task's background rule: a listed prefix, or an explicitly listed (or fixed background) module."""
    bg = task["background"]
    return in_background(module, bg["modules"]) or module in bg.get("explicit_modules", []) \
        or module in bg.get("fixed_modules", [])


# an import command, also in the module system's forms (`public`/`private`, `meta`, `import all`): its module is the
# last word; `all` is not a module (Lean 4.34.1 sources write `import all M`)
IMPORT_RE = re.compile(r"^\s*(?:(?:public|private)\s+)?(?:meta\s+)?import\s+(?:all\s+)?(\S+)", re.M)


# `set_option Elab.async false` at the start of a line (alone or as `... in`): Lean then elaborates the file's proofs on
# one thread instead of several (a ramsey96 build took 417 s instead of 102 s, cpu-profile 2026-10-04)
ELAB_ASYNC_OFF_RE = re.compile(r"^[ \t]*set_option[ \t]+Elab\.async[ \t]+false\b", re.M)


def text_lints(task: dict, sources: dict[str, str]) -> list[str]:
    """The forbidden-text list is a lint only: validity comes from the export (axioms, statements, kernel replay).
    `set_option Elab.async false` is legal and changes nothing that is checked or scored; it only slows the build."""
    lints = [f"forbidden text {word!r} in {rel}" for rel, text in sorted(sources.items())
             for word in task["rules"].get("forbidden_text", []) if word in text]
    lints += [f"{rel} sets `set_option Elab.async false`: Lean then elaborates it on one thread, so the build takes "
              f"longer (about 4x on ramsey96); this is allowed and does not change the score"
              for rel, text in sorted(sources.items()) if ELAB_ASYNC_OFF_RE.search(text)]
    return lints


def check_extraction(task: dict, data: dict, sources: dict[str, str]) -> list[str]:
    problems = []
    rules = task["rules"]
    owners = set(task["project"]["owner_modules"])
    for rel, text in sources.items():
        for imp in IMPORT_RE.findall(text):
            if imp not in owners and not task_in_background(task, imp):
                problems.append(f"import {imp} not allowed in {rel}")
    # every imported module is charged or background, so every constant the counter reaches is one of the two
    for m in data["owner_import_closure"]:
        if m not in owners and not task_in_background(task, m):
            problems.append(f"owner imports non-background module {m}")
    for ref in data.get("outside_refs", []):
        if not ref["module"] or not task_in_background(task, ref["module"]):
            problems.append(f"reference to non-background constant {ref['name']} ({ref['module']})")
    for name in data["missing_roots"]:
        problems.append(f"target missing: {name}")
    got = {t["name"]: t for t in data["targets"]}
    allowed_axioms = set(rules["allowed_axioms"])
    for t in task["targets"]:
        row = got.get(t["name"])
        if row is None:
            continue
        if row["kind"] != "theorem":
            problems.append(f"target {t['name']} is a {row['kind']}, not a theorem")
        if row["module"] not in owners:
            problems.append(f"target {t['name']} is not declared in an owner module")
        if statement_sha256(row["statement_ser"]) != t["statement_sha256"]:
            problems.append(f"target statement changed: {t['name']}")
        extra = sorted(set(row["axioms"]) - allowed_axioms)
        if extra:
            problems.append(f"target {t['name']} depends on disallowed axioms {extra}")
    frozen = {f["name"]: f for f in data["frozen"]}
    for f in task["frozen"]:
        row = frozen.get(f["name"])
        if row is None or row["missing"]:
            problems.append(f"frozen definition missing: {f['name']}")
        elif frozen_closure_sha256(row) != f["closure_sha256"]:
            problems.append(f"frozen definition changed: {f['name']}")
    return problems


# the bundle's scored file, at its top level: the kernel export, or the score breakdown for a task without one
BUNDLE_EXPORT_FILES = ("kernel_export.jsonl", "score_breakdown.jsonl")


def kernel_export_kept(task: dict) -> bool:
    """Whether the task's bundles carry the full kernel export (TASK.json `export.kernel_export`, default true)."""
    return task.get("export", {}).get("kernel_export", True)


def bundle_export_name(task: dict) -> str:
    return BUNDLE_EXPORT_FILES[0] if kernel_export_kept(task) else BUNDLE_EXPORT_FILES[1]


def scored_export(task_sha: str, data: dict) -> bytes:
    """The scored object: the metric counter's hash-consed kernel export of every charged declaration reached from
    the targets (lcbench.metric_export.v1); a task without kernel export gets the per-declaration score breakdown."""
    if data.get("export") is not None:
        return data["export"]
    lines = [canonical_json({"schema": "lcbench.score_breakdown.v1", "task_sha256": task_sha,
                             "metric": data["metric"], "owners": data["owners"]})]
    for row in sorted(data["declarations"], key=lambda r: r["name"]):
        lines.append(canonical_json({k: row[k] for k in ("name", "kind", "role", "cost")}))
    return b"".join(lines)


def score(task: dict, total: int) -> dict:
    baseline = task["metric"]["baseline_total"]
    out = {"metric": task["metric"]["id"], "total": total, "baseline": baseline,
           "reduction_pct": round(100.0 * (baseline - total) / baseline, 4)}
    if task["metric"].get("recorded"):
        out["recorded"] = task["metric"]["recorded"]
    return out


def scores(task: dict, data: dict) -> dict:
    """Every metric id with its value from the export; a recorded metric this scorer does not compute is null."""
    out = {"primary": task["metric"]["id"], task["metric"]["id"]: data["total"]}
    for metric in RECORDED_METRICS:
        out.setdefault(metric, data.get(metric))
    return out


def evaluate(task: dict, task_sha: str, fixed_root: Path, editable_root: Path, workdir: Path,
             tools_dir: Path, packages_dir: Path | None = None, runner_factory=None,
             paranoid: bool | None = None) -> dict:
    """Full check: snapshot the sources, stage them at the canonical build path, build, kernel replay, export,
    rule checks, scores. `tools_dir` holds the organizer Lean tool sources; `runner_factory(toolchain, task_sha)` gives
    the runner (default: LocalRunner, no sandbox); `paranoid` (default LCBENCH_PARANOID=1) adds stock leanchecker."""
    workdir.mkdir(parents=True, exist_ok=True)
    result = {"valid": False, "problems": [], "lints": [], "score": None, "scores": None, "timings": {},
              "infrastructure_error": None, "heartbeats": None}
    toolchain, problems = resolve_toolchain(task, fixed_root)
    result["problems"] = problems
    if problems:
        return result
    snapshot = workdir / "sources"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    for rel in task["project"]["editable"]:
        if (editable_root / rel).is_file():
            (snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(editable_root / rel, snapshot / rel)
    result["sources_dir"] = snapshot
    runner = (runner_factory or LocalRunner)(toolchain, task_sha)
    limits = task_limits(task)
    runner.mem_mb, runner.max_processes, runner.disk_mb = limits["mem_mb"], limits["max_processes"], limits["disk_mb"]
    result["sandbox"] = runner.sandbox
    result["build_path"] = str(runner.inside_project)
    with runner:
        problems += stage_project(task, fixed_root, snapshot, runner.project, packages_dir)
        if problems:
            return result
        sources = {rel: (snapshot / rel).read_text() for rel in task["project"]["editable"]}
        result["source_tree_sha256"] = tree_sha256(snapshot, task["project"]["editable"])
        result["lints"] = text_lints(task, sources)
        built = build_and_extract(task, runner.project, tools_dir, runner, fixed_root.parent / BACKGROUND_MANIFEST,
                                  paranoid_default() if paranoid is None else paranoid)
        result["timings"] = built["timings"]
        result["heartbeats"] = built.get("heartbeats")
        result["kernel_replay_mode"] = built.get("kernel_replay_mode")
        result["peak_rss_mb"] = round(runner.stats.get("peak_mem_kb", 0) / 1024, 1)
        result["peak_tree_rss_mb"] = round(runner.stats.get("peak_tree_rss_kb", 0) / 1024, 1)
        result["memory_source"] = memory_source()
        result["peak_processes"] = runner.stats.get("peak_processes")
        result["peak_disk_mb"] = runner.stats.get("peak_disk_mb")
        if built["stage"] == "infrastructure":
            # not a verdict on the submission: no problem is recorded, and callers must re-run it elsewhere
            result["infrastructure_error"] = built["log"]
            return result
        if built["stage"] == "limit":
            key = built["limit"]
            problems.append(f"limit: {key} exceeded ({task_limits(task)[key]} {LIMIT_UNITS[key]})"
                            + (f" by {built['declaration']}" if built.get("declaration") else ""))
            return result
        if built["stage"] == "background":
            problems += built["problems"]
            return result
        if built["stage"] != "ok":
            problems.append(f"{built['stage']} failed" + (f": {built['log'].splitlines()[0]}"
                                                           if built["stage"] == "metric" and built["log"] else ""))
            result["log"] = built["log"]
            if built["stage"] == "metric":
                problems += check_extraction(task, built["data"], sources)
            return result
    data = built["data"]
    problems += check_extraction(task, data, sources)
    export = scored_export(task_sha, data)
    result.update(
        valid=not problems,
        score=score(task, data["total"]),
        scores=scores(task, data),
        declarations=[{k: r[k] for k in ("name", "kind", "role", "cost")} for r in data["declarations"]],
        kernel_export=export,
        kernel_export_sha256=sha256_bytes(export),
        statement_closure=data.get("statement_closure", []),
        kernel_replay_modules=task["project"]["owner_modules"],
        kernel_replay=built["replay"])
    return result


def check_report(task: dict, task_sha: str, res: dict) -> dict:
    """What `./check --json` prints: identical wherever the package lives (no timings; paths only under the canonical
    build path). A failed stage carries the end of its Lean/Lake output in `log_tail`, as the receipt does."""
    return {"schema": CHECK_SCHEMA, "scorer_version": SCORER_VERSION, "task_name": task["name"],
            "task_sha256": task_sha, "metric_ids": metric_ids(task), "valid": res["valid"],
            "problems": res["problems"], "lints": res["lints"], "score": res["score"], "scores": res["scores"],
            "source_tree_sha256": res.get("source_tree_sha256"), "kernel_export_sha256": res.get("kernel_export_sha256"),
            "build_path": res.get("build_path"),
            "kernel_replay": res.get("kernel_replay_mode"), "infrastructure_error": res.get("infrastructure_error"),
            "log_tail": (res.get("log") or "")[-4000:] or None}


# exit status of check, submit and verify when a safety limit stopped the evaluation: no verdict, re-run elsewhere
INFRASTRUCTURE_EXIT = 3


def exit_status(res: dict) -> int:
    return 0 if res["valid"] else INFRASTRUCTURE_EXIT if res.get("infrastructure_error") else 1


def metric_ids(task: dict) -> list[str]:
    return [task["metric"]["id"], *RECORDED_METRICS]


# ---------------------------------------------------------------- participant commands

def load_package(package: Path) -> tuple[dict, str]:
    raw = (package / "TASK.json").read_bytes()
    return json.loads(raw), sha256_bytes(raw)


def package_packages_dir(package: Path, task: dict) -> Path | None:
    link = package / task["project"]["root"] / ".lake/packages"
    return link.resolve() if task["project"].get("packages") and link.exists() else None


def objective_line(task: dict) -> str:
    """The second line of ./check: the scorer and the one objective (recorded metrics are not objectives); the
    metric id appears once, on the result line below."""
    return f"scorer mcb {SCORER_VERSION}  objective: proof-term node count (smaller is better)"


def compression_summary(score: dict) -> str:
    """How results are reported: baseline / node count, the node count of the submission, and what it removed."""
    total, baseline = score["total"], score["baseline"]
    factor = f"{baseline / total:.2f}x" if total > 0 else "n/a"
    return f"compression factor {factor}  nodes remaining {total}  nodes removed {baseline - total}"


def print_result(res: dict) -> None:
    if res.get("infrastructure_error"):
        print("NO VERDICT:", res["infrastructure_error"])
        return
    status = "VALID" if res["valid"] else "INVALID"
    s = res["score"]
    if s:
        print(f"{status}  {s['metric']}: total={s['total']} baseline={s['baseline']} "
              f"reduction={s['reduction_pct']}%")
        print("  " + compression_summary(s))
    else:
        print(status)
    for p in res["problems"]:
        print("  PROBLEM:", p)
    for p in res.get("lints", []):
        print("  LINT (not a rejection):", p)
    if res.get("log"):
        print(res["log"][-3000:])


def run_evaluate(args, package: Path, task: dict, task_sha: str, default_work: str) -> dict:
    project = Path(args.project).resolve() if args.project else package / task["project"]["root"]
    workdir = Path(args.workdir).resolve() if args.workdir else package / ".work" / default_work
    return evaluate(task, task_sha, package / task["project"]["root"], project, workdir,
                    package / ".benchmark/tools", package_packages_dir(package, task), paranoid=args.paranoid)


def cmd_check(args) -> int:
    package = Path(args.package).resolve()
    task, task_sha = load_package(package)
    res = run_evaluate(args, package, task, task_sha, "check")
    if args.json:
        sys.stdout.write(pretty_json(check_report(task, task_sha, res)))
        return exit_status(res)
    print(f"task {task['name']}  sha256 {task_sha}  mode {task['mode']}")
    print(objective_line(task))
    print_result(res)
    if res.get("declarations") and args.verbose:
        for r in sorted(res["declarations"], key=lambda r: -r["cost"]):
            print(f"  {r['cost']:6d}  {r['role']:6} {r['kind']:11} {r['name']}")
    return exit_status(res)


TRACKS = ("agent", "record")
RUN_SCHEMA = "lcbench.run.v1"
RUN_FILE = "RUN.json"
UNLIMITED = "unlimited"


def utc_stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_record(task: dict, task_sha: str, budget_seconds: int | None, track: str | None, now: datetime) -> dict:
    """RUN.json: the budget of one checkout (a number of seconds, or unlimited), its entry track and its start time.
    The budget belongs to the checkout, not to the task; whoever runs the participant enforces it."""
    if budget_seconds is not None and (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, int)
                                       or budget_seconds <= 0):
        raise ValueError(f"budget must be a positive whole number of seconds, got {budget_seconds!r}")
    if track not in (None, *TRACKS):
        raise ValueError(f"unknown track {track!r} (allowed: {', '.join(TRACKS)})")
    return {"schema": RUN_SCHEMA, "task_name": task["name"], "task_sha256": task_sha,
            "budget_seconds": UNLIMITED if budget_seconds is None else budget_seconds, "track": track,
            "started_at": utc_stamp(now), "scorer_version": SCORER_VERSION}


def write_run(package: Path, run: dict) -> Path:
    path = package / RUN_FILE
    path.write_text(pretty_json(run))
    return path


def read_run(package: Path) -> dict | None:
    path = package / RUN_FILE
    return json.loads(path.read_text()) if path.is_file() else None


def run_metadata(run: dict | None, tokens: dict | None, now: datetime) -> dict | None:
    """What ./submit records about the run in SUBMISSION.json (metadata.run): the checkout's budget, the wall time
    since its start, and the declared token usage. None when there is neither a RUN.json nor declared tokens."""
    if run is None and tokens is None:
        return None
    if run is None:
        return {"budget_seconds": UNLIMITED, "started_at": None, "elapsed_seconds": None, "tokens": tokens,
                "run_json_sha256": None}
    started = datetime.strptime(run["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {"budget_seconds": run["budget_seconds"], "started_at": run["started_at"],
            "elapsed_seconds": int((now - started).total_seconds()), "tokens": tokens,
            "run_json_sha256": sha256_bytes(canonical_json(run))}


def build_bundle(task: dict, task_sha: str, project: Path, res: dict, out_dir: Path, stage: Path,
                 trajectory: Path | None = None, track: str | None = None,
                 run: dict | None = None) -> tuple[Path, str]:
    """Write the submission bundle (sources, the kernel export at the top level, optional agent log, claims, the entry
    track and run metadata (budget, elapsed time, tokens), manifest). Compiled .olean files are not part of it."""
    if stage.exists():
        shutil.rmtree(stage)
    for rel in task["project"]["editable"]:
        (stage / "source" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / rel, stage / "source" / rel)
    stage.mkdir(parents=True, exist_ok=True)
    (stage / bundle_export_name(task)).write_bytes(res["kernel_export"])
    traj = {"present": False}
    if trajectory is not None:
        files = list_files(trajectory)
        for rel in files:
            (stage / "trajectory" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(trajectory / rel, stage / "trajectory" / rel)
        traj = {"present": True, "files": len(files), "tree_sha256": tree_sha256(trajectory, files)}
    submission = {
        "schema": SUBMISSION_SCHEMA,
        "task_name": task["name"], "task_sha256": task_sha,
        "toolchain": {"id": task["toolchain"]["id"], "lean_githash": task["toolchain"]["lean_githash"]},
        "editable": task["project"]["editable"],
        "source_tree_sha256": res["source_tree_sha256"],
        "claimed_score": res["score"],
        "claimed_scores": res["scores"],
        "kernel_export_sha256": res["kernel_export_sha256"],
        "trajectory": traj,
        "metadata": {"track": track, **({"run": run} if run is not None else {})},
        "local_check": {"valid": res["valid"], "benchkit_sha256": sha256_file(HERE / "benchkit.py")},
    }
    (stage / "SUBMISSION.json").write_text(pretty_json(submission))
    write_manifest(stage)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "submission.tar.gz.partial"
    bundle_sha = deterministic_tar(stage, tmp, "submission")
    final = out_dir / f"submission-{bundle_sha[:16]}.tar.gz"
    tmp.replace(final)
    return final, bundle_sha


def submit_run_problems(run: dict | None, task_sha: str, track: str | None) -> list[str]:
    """RUN.json must belong to this task, and a --track given to ./submit must agree with the one it declares."""
    if run is None:
        return []
    problems = []
    if run.get("task_sha256") != task_sha:
        problems.append(f"{RUN_FILE} was written for another task (task_sha256 {run.get('task_sha256')})")
    if track is not None and run.get("track") not in (None, track):
        problems.append(f"--track {track} differs from the track in {RUN_FILE} ({run['track']})")
    return problems


def cmd_submit(args) -> int:
    package = Path(args.package).resolve()
    task, task_sha = load_package(package)
    trajectory = Path(args.log).resolve() if args.log else None
    if trajectory is not None and not trajectory.is_dir():
        print(f"--log {trajectory} is not a directory")
        return 1
    tokens = None
    if args.tokens_json:
        try:
            tokens = json.loads(Path(args.tokens_json).read_text())
        except (OSError, ValueError) as e:
            print(f"--tokens-json {args.tokens_json}: {e}")
            return 1
        if not isinstance(tokens, dict):
            print(f"--tokens-json {args.tokens_json}: expected a JSON object")
            return 1
    run = read_run(package)
    problems = submit_run_problems(run, task_sha, args.track)
    if problems:
        for p in problems:
            print("  PROBLEM:", p)
        return 1
    track = args.track or (run or {}).get("track")
    res = run_evaluate(args, package, task, task_sha, "submit")
    print_result(res)
    if res.get("infrastructure_error"):
        print("no bundle: the check did not finish on this machine (rerun ./submit, or on a machine with more time, "
              "memory or disk)")
        return INFRASTRUCTURE_EXIT
    if not res["valid"]:
        print("refusing to bundle an invalid submission (fix the problems above and rerun ./submit)")
        return 1
    out_dir = Path(args.out).resolve() if args.out else package / "submissions"
    workdir = Path(args.workdir).resolve() if args.workdir else package / ".work" / "submit"
    run_meta = run_metadata(run, tokens, datetime.now(timezone.utc))
    bundle, bundle_sha = build_bundle(task, task_sha, res["sources_dir"], res, out_dir, workdir / "bundle",
                                      trajectory, track, run_meta)
    if run_meta is not None:
        print(f"run: budget {run_meta['budget_seconds']}, elapsed {run_meta['elapsed_seconds']} s, "
              f"track {track or 'none'}")
    print(f"bundle: {bundle}")
    print(f"submission ID (sha256 of bundle): {bundle_sha}")
    print("Submitting (nothing was sent):")
    for line in task["submission_channels"]:
        print("  -", line.replace("{bundle}", bundle.name).replace("{id}", bundle_sha))
    return 0


def fetch(url_or_path: str) -> bytes:
    if "://" in url_or_path:
        with urllib.request.urlopen(url_or_path, timeout=600) as r:
            return r.read()
    return Path(url_or_path).read_bytes()


# the files that make a Lake project's dependency closure; a folder with copies of them fetches the same packages
LAKE_FILES = ("lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain")


def fetch_packages(project: Path, deps: Path, task: dict) -> str | None:
    """Fetch the pinned Lake packages and Mathlib's compiled cache into deps/.lake/packages: `lake exe cache get` in
    `deps`, a folder holding copies of the project's Lake files only (the README's second way). Run inside the project,
    a project with a Mathlib/ folder (the editable Mathlib modules of mathlib197 and mathlib372) made Mathlib's cache
    tool take the project for Mathlib itself, and the cache never reached the mathlib package. Returns a problem, or
    None when the packages are fetched."""
    deps.mkdir(parents=True, exist_ok=True)
    for name in LAKE_FILES:
        if (project / name).is_file():
            shutil.copyfile(project / name, deps / name)
    print(f"lake exe cache get (in {deps})", flush=True)
    try:
        got = run(["lake", "exe", "cache", "get"], deps, lean_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return (f"lake exe cache get could not run ({e}): install {task['toolchain']['id']} (elan), "
                "or start with --deps DIR")
    if got.returncode != 0:
        return "lake exe cache get failed:\n" + (got.stdout + got.stderr)[-3000:]
    return None


def link_packages(project: Path, deps: Path) -> None:
    link = project / ".lake/packages"
    link.parent.mkdir(exist_ok=True)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(deps.resolve())


def cmd_start(args) -> int:
    """The one participant command: fetch, hash-check, unpack, set up the pinned project, print the task."""
    dest = Path(args.dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    raw = fetch(args.package)
    got = sha256_bytes(raw)
    want = args.sha256
    if want is None:
        try:
            want = fetch(args.package + ".sha256").decode().split()[0]
        except (OSError, ValueError, IndexError):
            want = None
    if want and want != got:
        print(f"REFUSED: package sha256 {got} differs from published {want}")
        return 1
    tmp = dest / ".lcbench-download.tar.gz"
    tmp.write_bytes(raw)
    try:
        package = safe_extract(tmp, dest)
    finally:
        tmp.unlink()
    problems = check_manifest(package)
    task, task_sha = load_package(package)
    for rel, digest in task["tooling_sha256"].items():
        if sha256_file(package / rel) != digest:
            problems.append(f"tooling file differs from TASK.json: {rel}")
    project = package / task["project"]["root"]
    problems += toolchain_problems(task, project)
    if task["project"].get("packages"):
        if args.deps:
            link_packages(project, Path(args.deps))
        else:
            deps = dest / f"{package.name}-deps"
            fetch_problem = fetch_packages(project, deps, task)
            if fetch_problem:
                problems.append(fetch_problem)
            else:
                link_packages(project, deps / ".lake/packages")
                print(f"dependencies: {deps / '.lake/packages'} (pass it with --deps to start another copy)")
        problems += package_problems(task, package_packages_dir(package, task))
    print(f"package {package}")
    print(f"package sha256 {got}" + ("  (matches published)" if want else "  (no published hash to compare)"))
    print(f"task {task['name']}  TASK.json sha256 {task_sha}")
    for p in problems:
        print("  PROBLEM:", p)
    run_info = run_record(task, task_sha, args.budget_seconds, args.track, datetime.now(timezone.utc))
    write_run(package, run_info)
    print(f"{RUN_FILE}: budget {run_info['budget_seconds']}" + (" seconds" if args.budget_seconds else "")
          + f", track {run_info['track'] or 'none'}, started {run_info['started_at']}")
    print((package / "TASK.md").read_text() if (package / "TASK.md").is_file() else "")
    return 1 if problems else 0


def positive_seconds(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("the budget must be a positive number of seconds")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="mcb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start", help="fetch and set up a task package")
    st.add_argument("package", help="task package tarball URL (https://, file://) or path")
    st.add_argument("--dir", default=".", help="directory to unpack into (default: current)")
    st.add_argument("--sha256", help="published sha256 of the tarball (default: <package>.sha256 if present)")
    st.add_argument("--deps", help="read-only directory holding the pinned Lake packages (skips lake exe cache get)")
    budget = st.add_mutually_exclusive_group()
    budget.add_argument("--budget-seconds", type=positive_seconds,
                        help="this checkout's time budget, written to RUN.json (enforced by whoever runs the "
                             "participant, not by the task)")
    budget.add_argument("--unlimited", action="store_true", help="no time budget (the default)")
    st.add_argument("--track", choices=TRACKS, help="entry track of this checkout, written to RUN.json")
    for name in ("check", "submit"):
        p = sub.add_parser(name)
        p.add_argument("--package", required=True)
        p.add_argument("--project", help="project directory holding the editable sources (default: package/project)")
        p.add_argument("--workdir")
        p.add_argument("--paranoid", action="store_true", default=None,
                       help="also replay every owner module with stock leanchecker (slow; default LCBENCH_PARANOID=1)")
        if name == "check":
            p.add_argument("--verbose", action="store_true")
            p.add_argument("--json", action="store_true", help="print the full result as JSON (same fields as verify)")
        else:
            p.add_argument("--out", help="directory for the bundle (default: package/submissions)")
            p.add_argument("--log", help="optional agent trajectory/log directory to include (hashed)")
            p.add_argument("--track", choices=TRACKS,
                           help="entry track: agent (a model run) or record (any method); recorded in the receipt "
                                "(default: the track in RUN.json)")
            p.add_argument("--tokens-json", help="JSON object with the token usage you declare; recorded in the bundle")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return {"start": cmd_start, "check": cmd_check, "submit": cmd_submit}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
