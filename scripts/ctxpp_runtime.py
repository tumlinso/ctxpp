from __future__ import annotations

import atexit
import concurrent.futures
import contextlib
import json
import math
import os
import resource
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import fcntl
import functools


QUERY_SCHEMA = 1
_COUNTERS: dict[str, int] = defaultdict(int)
_TIMINGS_NS: dict[str, int] = defaultdict(int)
_PROFILE_PATH = os.environ.get("CTXPP_PROFILE_PATH")
_PROFILE_LOCK = threading.Lock()


def count(name: str, value: int = 1) -> None:
    if _PROFILE_PATH:
        with _PROFILE_LOCK:
            _COUNTERS[name] += value


@contextlib.contextmanager
def span(name: str):
    if not _PROFILE_PATH:
        yield
        return
    started = time.perf_counter_ns()
    try:
        yield
    finally:
        with _PROFILE_LOCK:
            _TIMINGS_NS[name] += time.perf_counter_ns() - started


def timed(name: str):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def profile_snapshot() -> dict[str, Any]:
    return {
        "format": "CTXPP-PROFILE/1",
        "counters": dict(sorted(_COUNTERS.items())),
        "timings_ns": dict(sorted(_TIMINGS_NS.items())),
    }


def _write_profile() -> None:
    if not _PROFILE_PATH:
        return
    destination = Path(_PROFILE_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(profile_snapshot(), sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


atexit.register(_write_profile)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _parse_cpu_set(value: str) -> int | None:
    if not value:
        return None
    total = 0
    try:
        for item in value.split(","):
            bounds = item.split("-", 1)
            total += int(bounds[-1]) - int(bounds[0]) + 1
        return total
    except ValueError:
        return None


def _memory_info() -> tuple[int, int, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return values.get("MemTotal", 0), values.get("MemAvailable", 0), values.get("SwapFree", 0)


@dataclass(frozen=True)
class ResourceEnvelope:
    logical_cpus: int
    physical_cpus: int
    affinity_cpus: int
    quota_cpus: int
    effective_cpus: int
    cpu_budget: int
    numa_nodes: int
    host_memory: int
    memory_limit: int
    available_memory: int
    memory_budget: int


def detect_resources(configured_worker_ceiling: int | None = None) -> ResourceEnvelope:
    logical = max(1, os.cpu_count() or 1)
    try:
        affinity_set = set(os.sched_getaffinity(0))
        affinity = max(1, len(affinity_set))
    except (AttributeError, OSError):
        affinity_set = set(range(logical))
        affinity = logical
    physical_ids = set()
    for cpu in affinity_set:
        core = _read_text(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id")
        package = _read_text(f"/sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id")
        if core:
            physical_ids.add((package, core))
    physical = len(physical_ids) or affinity
    try:
        numa_nodes = max(1, len(list(Path("/sys/devices/system/node").glob("node[0-9]*"))))
    except OSError:
        numa_nodes = 1
    cpuset = _parse_cpu_set(_read_text("/sys/fs/cgroup/cpuset.cpus.effective")) or affinity
    quota = logical
    cpu_max = _read_text("/sys/fs/cgroup/cpu.max").split()
    if len(cpu_max) == 2 and cpu_max[0] != "max":
        try:
            quota = max(1, math.floor(int(cpu_max[0]) / int(cpu_max[1])))
        except (ValueError, ZeroDivisionError):
            pass
    effective = max(1, min(logical, affinity, cpuset, quota))
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    outside_load = max(0.0, load - effective * 0.1)
    cpu_budget = max(1, min(math.ceil(effective * 0.9), math.floor(effective - min(effective * 0.75, outside_load))))
    manual_workers = os.environ.get("CTXPP_MAX_WORKERS")
    ceilings = [x for x in (configured_worker_ceiling, int(manual_workers) if manual_workers and manual_workers.isdigit() else None) if x]
    if ceilings:
        cpu_budget = min(cpu_budget, *ceilings)

    host_total, host_available, _ = _memory_info()
    raw_limit = _read_text("/sys/fs/cgroup/memory.max")
    try:
        cgroup_limit = int(raw_limit) if raw_limit and raw_limit != "max" else host_total
    except ValueError:
        cgroup_limit = host_total
    memory_limit = min(x for x in (host_total, cgroup_limit) if x > 0) if host_total or cgroup_limit else 8 * 1024**3
    current_raw = _read_text("/sys/fs/cgroup/memory.current")
    try:
        cgroup_available = max(0, memory_limit - int(current_raw)) if current_raw else memory_limit
    except ValueError:
        cgroup_available = memory_limit
    available = min(x for x in (host_available, cgroup_available) if x > 0) if host_available or cgroup_available else memory_limit
    reserve = max(2 * 1024**3, int(memory_limit * 0.15))
    desired = available - reserve if available > reserve else available // 2
    memory_budget = min(available, max(min(64 * 1024**2, available), desired))
    manual_memory = os.environ.get("CTXPP_MAX_MEMORY")
    if manual_memory:
        try:
            memory_budget = min(memory_budget, int(manual_memory))
        except ValueError:
            pass
    return ResourceEnvelope(logical, physical, affinity, quota, effective, cpu_budget, numa_nodes,
                            host_total, memory_limit, available, memory_budget)


def _pressure_sample() -> tuple[int, int, int]:
    _, available, swap_free = _memory_info()
    limit_raw = _read_text("/sys/fs/cgroup/memory.max")
    current_raw = _read_text("/sys/fs/cgroup/memory.current")
    swap_current_raw = _read_text("/sys/fs/cgroup/memory.swap.current")
    try:
        if limit_raw and limit_raw != "max" and current_raw:
            available = min(available or int(limit_raw), max(0, int(limit_raw) - int(current_raw)))
        if swap_current_raw:
            swap_free = -int(swap_current_raw)
    except ValueError:
        pass
    major = 0
    try:
        for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
            if line.startswith("pgmajfault "):
                major = int(line.split()[1])
                break
    except (OSError, ValueError):
        pass
    return available, swap_free, major


class ResourceScheduler:
    """One invocation-wide CPU/RAM admission controller for expensive jobs."""

    def __init__(self, root: Path, configured_worker_ceiling: int | None = None, *, envelope: ResourceEnvelope | None = None):
        self.root = root
        self.envelope = envelope or detect_resources(configured_worker_ceiling)
        self.initial_worker_cap = 0
        self.peak_workers = 0
        self.backoffs = 0
        self.path = root / ".ctxpp/cache/scheduler.json"
        self.history_lock = root / ".ctxpp/cache/scheduler.lock"
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.history = payload.get("jobs", {}) if payload.get("format") == "CTXPP-SCHEDULER/1" else {}
            self.batches = payload.get("batches", []) if payload.get("format") == "CTXPP-SCHEDULER/1" else []
        except (OSError, json.JSONDecodeError):
            self.history = {}
            self.batches = []
        self.reservation_path = root / ".ctxpp/cache/resource-reservations.json"
        self.reservation_lock = root / ".ctxpp/cache/resource-reservations.lock"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def _reserve(self, memory_mb: int) -> str | None:
        self.reservation_lock.parent.mkdir(parents=True, exist_ok=True)
        with self.reservation_lock.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    payload = json.loads(self.reservation_path.read_text(encoding="utf-8"))
                    reservations = payload.get("reservations", [])
                except (OSError, json.JSONDecodeError):
                    reservations = []
                reservations = [item for item in reservations if self._pid_alive(int(item.get("pid", -1)))]
                used_cpu = len(reservations)
                used_memory = sum(int(item.get("memory_mb", 0)) for item in reservations)
                budget_mb = max(1, self.envelope.memory_budget // 1024**2)
                if used_cpu >= self.envelope.cpu_budget or (reservations and used_memory + memory_mb > budget_mb):
                    return None
                token = f"{os.getpid()}:{threading.get_ident()}:{time.monotonic_ns()}"
                reservations.append({"token": token, "pid": os.getpid(), "memory_mb": memory_mb})
                self.reservation_path.write_text(json.dumps({"format": "CTXPP-RESERVATIONS/1", "reservations": reservations},
                                                            sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                return token
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _release(self, token: str) -> None:
        try:
            with self.reservation_lock.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    try:
                        payload = json.loads(self.reservation_path.read_text(encoding="utf-8"))
                        reservations = payload.get("reservations", [])
                    except (OSError, json.JSONDecodeError):
                        reservations = []
                    reservations = [item for item in reservations if item.get("token") != token and self._pid_alive(int(item.get("pid", -1)))]
                    self.reservation_path.write_text(json.dumps({"format": "CTXPP-RESERVATIONS/1", "reservations": reservations},
                                                                sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def estimate(self, job: dict[str, Any]) -> tuple[float, int]:
        history = self.history.get(str(job["history_key"]), {})
        fallback_mb = int(job.get("memory_hint_mb", 1536 if job.get("cuda") else 512))
        fallback_mb += min(2048, int(job.get("size", 0)) // (256 * 1024) * 64)
        if history and history.get("input_fingerprint") == job.get("input_fingerprint", ""):
            return float(history.get("duration_ms_ewma", job.get("duration_hint_ms", 1000))), int(history.get("memory_mb_ewma", fallback_mb))
        return (max(float(job.get("duration_hint_ms", 1000)), float(history.get("duration_ms_ewma", 0)) * 0.75),
                max(fallback_mb, round(float(history.get("memory_mb_ewma", 0)) * 0.75)))

    def _save(self) -> None:
        destination = self.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.history_lock.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    current = json.loads(destination.read_text(encoding="utf-8"))
                    disk_jobs = current.get("jobs", {}) if current.get("format") == "CTXPP-SCHEDULER/1" else {}
                    disk_batches = current.get("batches", []) if current.get("format") == "CTXPP-SCHEDULER/1" else []
                except (OSError, json.JSONDecodeError):
                    disk_jobs, disk_batches = {}, []
                merged_jobs = dict(disk_jobs)
                for key, value in self.history.items():
                    if int(value.get("updated_ns", 0)) >= int(merged_jobs.get(key, {}).get("updated_ns", 0)):
                        merged_jobs[key] = value
                merged_batches = sorted(
                    {json.dumps(item, sort_keys=True): item for item in [*disk_batches, *self.batches]}.values(),
                    key=lambda item: int(item.get("recorded_ns", 0)),
                )[-12:]
                payload = {"format": "CTXPP-SCHEDULER/1", "jobs": dict(sorted(merged_jobs.items())), "batches": merged_batches}
                fd, temporary = tempfile.mkstemp(prefix=".scheduler.", suffix=".json", dir=destination.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                finally:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                self.history, self.batches = merged_jobs, merged_batches
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def run(self, jobs: list[dict[str, Any]], operation) -> list[Any]:
        if not jobs:
            return []
        annotated = [(self.estimate(job), job) for job in jobs]
        annotated.sort(key=lambda item: (-item[0][0], -item[0][1], str(item[1]["history_key"])))
        cpu_cap = min(len(jobs), self.envelope.cpu_budget)
        comparable = [item for item in self.batches if item.get("jobs", 0) >= max(4, len(jobs) // 2)]
        if comparable:
            best_rate = max(float(item.get("jobs_per_second", 0)) for item in comparable)
            efficient = [int(item["workers"]) for item in comparable if float(item.get("jobs_per_second", 0)) >= best_rate * 0.95]
            if efficient:
                cpu_cap = min(cpu_cap, min(efficient))
        if os.environ.get("CTXPP_DISABLE_AUTOTUNE"):
            cpu_cap = min(len(jobs), self.envelope.cpu_budget)
        memory_budget_mb = max(1, self.envelope.memory_budget // 1024**2)
        active_cap = cpu_cap
        self.initial_worker_cap = cpu_cap
        running: dict[concurrent.futures.Future, tuple[dict[str, Any], int, float, str]] = {}
        results: list[Any] = []
        admitted_mb = 0
        previous_pressure = _pressure_sample()
        batch_started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=cpu_cap, thread_name_prefix="ctxpp") as pool:
            while annotated or running:
                admitted = False
                while annotated and len(running) < active_cap:
                    chosen = next((i for i, (estimate, _) in enumerate(annotated)
                                   if admitted_mb + estimate[1] <= memory_budget_mb), None)
                    if chosen is None:
                        if running:
                            count("scheduler_admission_stalls")
                            break
                        chosen = 0  # make progress with one oversized job
                    (duration_hint, memory_mb), job = annotated.pop(chosen)
                    reservation = self._reserve(memory_mb)
                    if reservation is None:
                        annotated.insert(chosen, ((duration_hint, memory_mb), job))
                        count("scheduler_admission_stalls")
                        break
                    future = pool.submit(operation, job)
                    running[future] = (job, memory_mb, time.perf_counter(), reservation)
                    admitted_mb += memory_mb
                    self.peak_workers = max(self.peak_workers, len(running))
                    count("peak_concurrent_workers", max(0, len(running) - _COUNTERS.get("peak_concurrent_workers", 0)))
                    admitted = True
                if not running:
                    time.sleep(0.02)
                    continue
                done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    job, estimate_mb, started, reservation = running.pop(future)
                    self._release(reservation)
                    admitted_mb -= estimate_mb
                    result = future.result()
                    results.append(result)
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    observed_mb = max(int(job.get("memory_floor_mb", 64)),
                                      int(getattr(result, "peak_memory_mb", 0) or estimate_mb))
                    old = self.history.get(str(job["history_key"]), {})
                    self.history[str(job["history_key"])] = {
                        "duration_ms_ewma": round(0.35 * duration_ms + 0.65 * float(old.get("duration_ms_ewma", duration_ms)), 3),
                        "memory_mb_ewma": max(1, round(0.35 * observed_mb + 0.65 * float(old.get("memory_mb_ewma", observed_mb)))),
                        "input_fingerprint": job.get("input_fingerprint", ""),
                        "updated_ns": time.time_ns(),
                    }
                current = _pressure_sample()
                available, swap_free, major = current
                old_available, old_swap, old_major = previous_pressure
                severe = (swap_free < old_swap) or (major > old_major) or (available and available < max(512 * 1024**2, self.envelope.memory_limit // 20))
                moderate = available and available < max(1024**3, self.envelope.memory_limit // 10)
                if severe:
                    active_cap = max(1, active_cap // 2)
                    self.backoffs += 1
                    count("memory_pressure_backoffs")
                elif moderate:
                    active_cap = max(1, active_cap - max(1, active_cap // 4))
                    self.backoffs += 1
                    count("memory_pressure_backoffs")
                elif annotated and active_cap < cpu_cap:
                    active_cap += 1
                previous_pressure = current
        elapsed = max(0.000001, time.perf_counter() - batch_started)
        count("workers_started", self.peak_workers)
        self.batches.append({"jobs": len(jobs), "workers": self.peak_workers,
                             "jobs_per_second": round(len(jobs) / elapsed, 3), "recorded_ns": time.time_ns()})
        self._save()
        return results


@dataclass
class MeasuredProcess:
    completed: subprocess.CompletedProcess[str]
    peak_memory_mb: int


def run_process_measured(arguments: list[str], *, cwd: Path | None = None) -> MeasuredProcess:
    proc = subprocess.Popen(arguments, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    peak_kb = 0
    finished = threading.Event()

    def monitor() -> None:
        nonlocal peak_kb
        status = Path(f"/proc/{proc.pid}/status")
        while not finished.wait(0.02):
            try:
                for line in status.read_text(encoding="utf-8").splitlines():
                    if line.startswith(("VmHWM:", "VmRSS:")):
                        peak_kb = max(peak_kb, int(line.split()[1]))
            except (OSError, ValueError):
                return

    watcher = threading.Thread(target=monitor, name="ctxpp-rss", daemon=True)
    watcher.start()
    stdout, stderr = proc.communicate()
    finished.set()
    watcher.join(timeout=0.1)
    return MeasuredProcess(subprocess.CompletedProcess(arguments, proc.returncode, stdout, stderr), max(1, math.ceil(peak_kb / 1024)))


class QueryStore:
    def __init__(self, connection):
        import sqlite3
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def exact(self, target: str, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data FROM symbols WHERE id=? OR qualified_name=? OR name=? "
            "ORDER BY (qualified_name<>?) ASC, definition DESC, file ASC LIMIT ?",
            (target, target, target, target, limit),
        ).fetchall()
        count("query_rows", len(rows))
        return [json.loads(row[0]) for row in rows]

    def location(self, file: str, line: int, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data FROM symbols WHERE file=? AND line<=? AND end_line>=? "
            "ORDER BY (end_byte-start_byte),qualified_name LIMIT ?",
            (file, line, line, limit),
        ).fetchall()
        count("query_rows", len(rows))
        return [json.loads(row[0]) for row in rows]

    def candidates(self, terms: list[str]) -> list[dict[str, Any]]:
        if not terms:
            return []
        clauses = " OR ".join("instr(search_text,?)>0" for _ in terms)
        rows = self.connection.execute(f"SELECT data FROM symbols WHERE {clauses}", tuple(terms)).fetchall()
        count("query_rows", len(rows))
        return [json.loads(row[0]) for row in rows]

    def symbol_ids(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        values = sorted(set(ids))
        if not values:
            return {}
        marks = ",".join("?" for _ in values)
        rows = self.connection.execute(f"SELECT id,data FROM symbols WHERE id IN ({marks})", values).fetchall()
        count("query_rows", len(rows))
        return {row[0]: json.loads(row[1]) for row in rows}

    def edges_for(self, symbol_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data FROM edges WHERE from_id=? OR to_id=? ORDER BY edge_order", (symbol_id, symbol_id)
        ).fetchall()
        count("query_rows", len(rows))
        return [json.loads(row[0]) for row in rows]

    def test_symbols(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT data FROM symbols WHERE is_test=1 ORDER BY symbol_order").fetchall()
        count("query_rows", len(rows))
        return [json.loads(row[0]) for row in rows]

    def all_symbols_edges(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        symbols = [json.loads(row[0]) for row in self.connection.execute("SELECT data FROM symbols ORDER BY symbol_order")]
        edges = [json.loads(row[0]) for row in self.connection.execute("SELECT data FROM edges ORDER BY edge_order")]
        count("query_rows", len(symbols) + len(edges))
        return symbols, edges

    def file(self, path: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT data FROM files WHERE path=?", (path,)).fetchone()
        count("query_rows", int(row is not None))
        return json.loads(row[0]) if row else None

    def files(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT data FROM files ORDER BY file_order").fetchall()
        count("query_rows", len(rows))
        return [json.loads(row[0]) for row in rows]

    def meta(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT value FROM metadata WHERE key='index_meta'").fetchone()
        return json.loads(row[0]) if row else {}

    def counts(self) -> tuple[int, int, int]:
        return tuple(int(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                     for table in ("files", "symbols", "edges"))


def query_store_path(root: Path) -> Path:
    return root / ".ctxpp/cache/query.sqlite"


def open_query_store(root: Path) -> QueryStore | None:
    import sqlite3
    database = query_store_path(root)
    manifest = root / ".ctxpp/manifest.json"
    if not database.is_file() or not manifest.is_file():
        count("query_store_misses")
        return None
    try:
        expected = json.loads(manifest.read_text(encoding="utf-8"))["index_hash"]
        connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        row = connection.execute("SELECT value FROM metadata WHERE key='index_hash'").fetchone()
        version = connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not row or row[0] != expected or not version or int(version[0]) != QUERY_SCHEMA:
            connection.close()
            count("query_store_misses")
            return None
        count("query_store_hits")
        return QueryStore(connection)
    except (OSError, json.JSONDecodeError, KeyError, sqlite3.Error, ValueError):
        count("query_store_misses")
        return None


@timed("query_store_build")
def build_query_store(root: Path, index_hash: str, files: list[dict[str, Any]], symbols: list[dict[str, Any]], edges: list[dict[str, Any]],
                      meta: dict[str, Any] | None = None) -> None:
    import sqlite3
    destination = query_store_path(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".query.", suffix=".sqlite", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE files(path TEXT PRIMARY KEY,file_order INTEGER NOT NULL,data TEXT NOT NULL);"
            "CREATE TABLE symbols(id TEXT PRIMARY KEY,name TEXT,qualified_name TEXT,kind TEXT,file TEXT,line INTEGER,end_line INTEGER,"
            "start_byte INTEGER,end_byte INTEGER,definition INTEGER,is_test INTEGER,search_text TEXT,symbol_order INTEGER NOT NULL,data TEXT NOT NULL);"
            "CREATE TABLE edges(from_id TEXT,to_id TEXT,type TEXT,edge_order INTEGER NOT NULL,data TEXT NOT NULL);"
            "CREATE INDEX symbols_name ON symbols(name); CREATE INDEX symbols_qname ON symbols(qualified_name);"
            "CREATE INDEX symbols_file_line ON symbols(file,line,end_line); CREATE INDEX edges_from ON edges(from_id); CREATE INDEX edges_to ON edges(to_id);"
        )
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (("schema", str(QUERY_SCHEMA)), ("index_hash", index_hash),
             ("index_meta", json.dumps(meta or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))),
        )
        connection.executemany(
            "INSERT INTO files VALUES(?,?,?)",
            ((record["path"], order, json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) for order, record in enumerate(files)),
        )
        connection.executemany(
            "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((
                record["id"], record.get("name", ""), record.get("qualified_name", ""), record.get("kind", ""), record.get("file", ""),
                int(record.get("line", 0)), int(record.get("end_line", record.get("line", 0))), int(record.get("start", 0)), int(record.get("end", 0)),
                int(bool(record.get("definition"))), int("test" in str(record.get("file", "")).lower() or "test" in str(record.get("name", "")).lower()),
                " ".join(str(record.get(key, "")) for key in ("qualified_name", "signature", "contract", "file", "lexical_terms")).lower(), order,
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ) for order, record in enumerate(symbols)),
        )
        connection.executemany(
            "INSERT INTO edges VALUES(?,?,?,?,?)",
            ((record.get("from", ""), record.get("to", ""), record.get("type", ""), order,
              json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) for order, record in enumerate(edges)),
        )
        connection.commit()
        connection.close()
        os.replace(temporary, destination)
        count("query_store_rebuilds")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
