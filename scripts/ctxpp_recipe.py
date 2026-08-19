from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


COMPILERS = {"nvcc", "clang++", "clang", "g++", "gcc", "c++", "cc"}
CUDA_EXTENSIONS = {".cu", ".cuh"}


def translate(record: dict[str, Any], source: Path) -> dict[str, Any]:
    directory = Path(record.get("directory", source.parent)).resolve()
    argv = _expand(_argv(record), directory)
    compiler = argv[0] if argv else ""
    nvcc = Path(compiler).name == "nvcc"
    args: list[str] = []
    translations: list[dict[str, str]] = []
    diagnostics: list[dict[str, str]] = []
    architecture = ""
    cuda_path = _cuda_path(compiler, argv)
    i = 1
    while i < len(argv):
        arg = argv[i]
        value = argv[i + 1] if i + 1 < len(argv) else ""
        if _same_source(arg, source, directory):
            i += 1; continue
        if arg in {"-o", "--output", "-MF", "-MT", "-MQ", "--keep-dir", "-Xptxas", "-Xlinker", "-L", "-l"}:
            translations.append({"option": arg, "action": "drop"}); i += 2; continue
        if arg in {"-M", "-MM", "-MD", "-MMD", "-c", "--compile", "-G", "-lineinfo", "--keep", "--use_fast_math"} or arg.startswith(("-O", "-l", "-L", "--threads=")):
            translations.append({"option": arg, "action": "drop"}); i += 1; continue
        if arg == "--threads":
            translations.append({"option": arg, "action": "drop"}); i += 2; continue
        if arg in {"-I", "-isystem", "-D", "-U", "-include"}:
            if value: args += [arg, value]
            i += 2; continue
        if arg == "--pre-include":
            if value: args += ["-include", value]
            translations.append({"option": arg, "action": "map"}); i += 2; continue
        if arg.startswith(("-I", "-D", "-U", "-std=", "--cuda-path=")) or arg in {"-pthread", "-fPIC"}:
            args.append(arg); i += 1; continue
        if arg == "--std" and value:
            args.append(f"-std={value}"); translations.append({"option": arg, "action": "map"}); i += 2; continue
        if arg.startswith("-arch=") or arg.startswith("--gpu-architecture="):
            architecture = arg.split("=", 1)[1].replace("compute_", "sm_"); i += 1; continue
        if arg in {"-arch", "--gpu-architecture"} and value:
            architecture = value.replace("compute_", "sm_"); i += 2; continue
        if arg in {"-gencode", "--generate-code"} and value:
            architecture = _arch(value) or architecture; translations.append({"option": arg, "action": "map"}); i += 2; continue
        if arg.startswith(("-gencode=", "--generate-code=")):
            architecture = _arch(arg.split("=", 1)[1]) or architecture; translations.append({"option": arg, "action": "map"}); i += 1; continue
        if arg in {"-Xcompiler", "--compiler-options"} and value:
            for host in _forwarded(value):
                if host.startswith(("-I", "-D", "-U", "-std=")) or host in {"-pthread", "-fPIC"}:
                    args.append(host)
            translations.append({"option": arg, "action": "unpack"}); i += 2; continue
        if arg in {"-ccbin", "--compiler-bindir"}:
            translations.append({"option": arg, "action": "drop"}); i += 2; continue
        if arg.startswith(("-ccbin=", "--compiler-bindir=")):
            translations.append({"option": arg, "action": "drop"}); i += 1; continue
        if arg in {"--expt-relaxed-constexpr", "--expt-extended-lambda", "-forward-unknown-to-host-compiler"}:
            translations.append({"option": arg, "action": "drop"}); i += 1; continue
        if arg.startswith(("-Xptxas=", "-Xlinker=")):
            translations.append({"option": arg, "action": "drop"}); i += 1; continue
        if arg.startswith(("-Xcompiler=", "--compiler-options=")):
            for host in _forwarded(arg.split("=", 1)[1]):
                if host.startswith(("-I", "-D", "-U", "-std=")) or host in {"-pthread", "-fPIC"}:
                    args.append(host)
            translations.append({"option": arg, "action": "unpack"}); i += 1; continue
        if nvcc and arg.startswith("-"):
            diagnostics.append({"category": "command_translation", "message": f"unsupported NVCC option {arg!r} omitted"})
            translations.append({"option": arg, "action": "unsupported"}); i += 1; continue
        args.append(arg); i += 1
    if nvcc or source.suffix in CUDA_EXTENSIONS:
        args = ["-x", "cuda", *args]
        if cuda_path and not any(arg.startswith("--cuda-path") for arg in args):
            args.append(f"--cuda-path={cuda_path}")
        if architecture:
            args.append(f"--cuda-gpu-arch={architecture}")
    elif Path(compiler).name in {"g++", "gcc", "c++"}:
        probe = subprocess.run([compiler, "-print-file-name=include"], text=True, capture_output=True, check=False)
        include = probe.stdout.strip()
        if probe.returncode == 0 and include and Path(include).is_dir():
            args += ["-isystem", include]
    return {"directory": str(directory), "original_argv": argv, "clang_argv": args,
            "compiler_identity": compiler, "translations": translations,
            "preparation_diagnostics": diagnostics}


def infer(source: Path, records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str, float]:
    same_cuda = source.suffix in CUDA_EXTENSIONS
    candidates = []
    for record in records:
        template = _record_source(record)
        if not template or (template.suffix in CUDA_EXTENSIONS) != same_cuda:
            continue
        common = len(os.path.commonpath([str(source.parent), str(template.parent)]))
        test_bonus = 20 if ("test" in source.parts) == ("test" in template.parts) else 0
        candidates.append((common + test_bonus, str(template), record))
    if not candidates:
        return None, "none", 0.0
    _, _, record = max(candidates, key=lambda item: (item[0], item[1]))
    inferred = dict(record)
    template = _record_source(record)
    argv = _argv(record)
    directory = Path(record.get("directory", source.parent)).resolve()
    inferred["arguments"] = [str(source) if template and _same_source(arg, template, directory) else arg for arg in argv]
    inferred.pop("command", None)
    inferred["file"] = str(source)
    return inferred, "inferred_sibling", 0.75


def preflight(recipe: dict[str, Any], source: Path) -> tuple[bool, str]:
    clang = os.environ.get("CTXPP_CLANG") or shutil.which("clang++") or shutil.which("clang")
    if not clang:
        return False, "Clang driver unavailable"
    proc = subprocess.run([clang, "-###", "-fsyntax-only", *recipe["clang_argv"], str(source)],
                          cwd=recipe["directory"], text=True, capture_output=True, check=False)
    if proc.returncode:
        lines = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
        return False, "\n".join(lines[:4])
    return True, ""


def observed_records(root: Path) -> list[dict[str, Any]]:
    path = root / ".ctxpp/cache/observed-commands.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return list(value.get("records", [])) if value.get("format") == "CTXPP-OBSERVED-COMMANDS/1" else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def successful_records(root: Path) -> list[dict[str, Any]]:
    path = root / ".ctxpp/cache/parse-recipes.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    records = []
    for record in value.get("records", []) if value.get("format") == "CTXPP-PARSE-RECIPES/1" else []:
        source = _record_source(record)
        if source and source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == record.get("source_hash"):
            records.append(record)
    return records


def persist_successful(root: Path, record: dict[str, Any], source: Path, origin: str,
                       write: Callable[[Path, bytes], None]) -> None:
    if origin == "compile_database":
        return
    saved = dict(record)
    saved.update({"file": str(source), "origin": origin,
                  "source_hash": hashlib.sha256(source.read_bytes()).hexdigest()})
    records = successful_records(root)
    records = [item for item in records if _record_source(item) != source.resolve()]
    records.append(saved)
    records.sort(key=lambda item: (str(_record_source(item)), json.dumps(item, sort_keys=True, default=str)))
    payload = {"format": "CTXPP-PARSE-RECIPES/1", "records": records}
    write(root / ".ctxpp/cache/parse-recipes.json", (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())


def capture_direct(root: Path, command: str, cwd: Path, succeeded: bool, write: Callable[[Path, bytes], None]) -> None:
    if not succeeded:
        return
    try:
        argv = shlex.split(command)
    except ValueError:
        return
    if not argv or Path(argv[0]).name not in COMPILERS:
        return
    sources = [arg for arg in argv[1:] if Path(arg).suffix in {".c", ".cc", ".cpp", ".cxx", ".cu"}]
    if not sources:
        return
    records = observed_records(root)
    for source in sources:
        records.append({"directory": str(cwd), "file": source, "arguments": argv, "origin": "observed_standalone"})
    unique = {json.dumps(record, sort_keys=True, separators=(",", ":")): record for record in records}
    payload = {"format": "CTXPP-OBSERVED-COMMANDS/1", "records": [unique[key] for key in sorted(unique)]}
    write(root / ".ctxpp/cache/observed-commands.json", (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())


def run_captured(root: Path, command: str, cwd: Path, write: Callable[[Path, bytes], None]) -> subprocess.CompletedProcess[str]:
    real = {name: path for name in COMPILERS if (path := shutil.which(name))}
    with tempfile.TemporaryDirectory(prefix="ctxpp-compilers-") as raw:
        wrapper_dir = Path(raw)
        log = wrapper_dir / "commands.jsonl"
        launcher = wrapper_dir / "launcher.py"
        launcher.write_text(
            "#!/usr/bin/env python3\nimport json,os,subprocess,sys\n"
            "real=json.loads(os.environ['CTXPP_REAL_COMPILERS']); name=os.path.basename(sys.argv[0])\n"
            "p=subprocess.run([real[name],*sys.argv[1:]])\n"
            "if p.returncode==0:\n"
            " r={'directory':os.getcwd(),'arguments':[real[name],*sys.argv[1:]],'exit_status':p.returncode}\n"
            " with open(os.environ['CTXPP_CAPTURE_LOG'],'a',encoding='utf-8') as f:f.write(json.dumps(r,separators=(',',':'))+'\\n')\n"
            "raise SystemExit(p.returncode)\n", encoding="utf-8")
        launcher.chmod(0o755)
        for name in real:
            (wrapper_dir / name).symlink_to(launcher.name)
        env = dict(os.environ)
        env.update({"PATH": f"{wrapper_dir}{os.pathsep}{env.get('PATH', '')}",
                    "CTXPP_REAL_COMPILERS": json.dumps(real), "CTXPP_CAPTURE_LOG": str(log)})
        proc = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, check=False, env=env)
        records = observed_records(root)
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                try: captured = json.loads(line)
                except json.JSONDecodeError: continue
                for arg in captured.get("arguments", [])[1:]:
                    if Path(arg).suffix in {".c", ".cc", ".cpp", ".cxx", ".cu"}:
                        records.append({"directory": captured["directory"], "file": arg,
                                        "arguments": captured["arguments"], "origin": "observed_standalone"})
        unique = {json.dumps(record, sort_keys=True, separators=(",", ":")): record for record in records}
        payload = {"format": "CTXPP-OBSERVED-COMMANDS/1", "records": [unique[key] for key in sorted(unique)]}
        write(root / ".ctxpp/cache/observed-commands.json", (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
        capture_direct(root, command, cwd, proc.returncode == 0, write)
        return proc


def _argv(record: dict[str, Any]) -> list[str]:
    return [str(value) for value in record["arguments"]] if isinstance(record.get("arguments"), list) else shlex.split(str(record.get("command", "")))


def _expand(argv: list[str], directory: Path) -> list[str]:
    result = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("@"):
            path = Path(arg[1:]); path = path if path.is_absolute() else directory / path
            try: result.extend(shlex.split(path.read_text(encoding="utf-8"), posix=True))
            except (OSError, ValueError): result.append(arg)
        elif arg == "--options-file" and index + 1 < len(argv):
            for raw in argv[index + 1].split(","):
                path = Path(raw); path = path if path.is_absolute() else directory / path
                try: result.extend(shlex.split(path.read_text(encoding="utf-8"), posix=True))
                except (OSError, ValueError): pass
            index += 1
        elif arg.startswith("--options-file="):
            for raw in arg.split("=", 1)[1].split(","):
                path = Path(raw); path = path if path.is_absolute() else directory / path
                try: result.extend(shlex.split(path.read_text(encoding="utf-8"), posix=True))
                except (OSError, ValueError): pass
        else: result.append(arg)
        index += 1
    return result


def _forwarded(value: str) -> list[str]:
    try: values = shlex.split(value)
    except ValueError: values = [value]
    return [part for item in values for part in item.split(",") if part]


def _same_source(arg: str, source: Path, directory: Path) -> bool:
    if arg.startswith("-"): return False
    try: return (Path(arg) if Path(arg).is_absolute() else directory / arg).resolve() == source.resolve()
    except OSError: return False


def _record_source(record: dict[str, Any]) -> Path | None:
    raw = str(record.get("file", ""))
    if not raw: return None
    path = Path(raw); return (path if path.is_absolute() else Path(record.get("directory", ".")) / path).resolve()


def _arch(value: str) -> str:
    for token in value.replace("[", ",").replace("]", ",").split(","):
        token = token.strip()
        if "sm_" in token: return "sm_" + token.split("sm_", 1)[1].split()[0]
    for token in value.split(","):
        if "compute_" in token: return "sm_" + token.split("compute_", 1)[1].split()[0]
    return ""


def _cuda_path(compiler: str, argv: list[str]) -> str:
    for arg in argv:
        if arg.startswith("--cuda-path="): return arg.split("=", 1)[1]
    if Path(compiler).name == "nvcc":
        resolved = shutil.which(compiler) or compiler
        path = Path(resolved).resolve()
        if path.parent.name == "bin": return str(path.parent.parent)
    for key in ("CUDA_HOME", "CUDA_PATH"):
        if os.environ.get(key): return os.environ[key]
    for candidate in (Path("/usr/local/cuda"), Path("/opt/nvidia/hpc_sdk/Linux_x86_64")):
        if candidate.exists(): return str(candidate)
    return ""
