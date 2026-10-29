"""Run a real RC circuit in ngspice batch mode."""

import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def find_ngspice() -> str:
    override = os.environ.get("NGSPICE_BIN")
    if override:
        executable = shutil.which(override)
        if executable:
            return executable
        raise RuntimeError(f"NGSPICE_BIN does not name an executable: {override}")
    executable = shutil.which("ngspice")
    if executable:
        return executable
    local = ROOT / "tools" / "ngspice" / "Spice64" / "bin" / "ngspice_con.exe"
    if local.is_file():
        return str(local)
    raise RuntimeError(
        "ngspice was not found. Add it to PATH or set NGSPICE_BIN to its "
        "batch executable (on Windows, prefer ngspice_con.exe)."
    )


def run_smoke() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    executable = find_ngspice()
    version = subprocess.run(
        [executable, "-v"], capture_output=True, text=True, timeout=15, check=False
    )
    if version.returncode != 0:
        raise RuntimeError(f"ngspice -v failed: {version.stdout}\n{version.stderr}")
    version_text = version.stdout + version.stderr
    version_line = next(
        (line.strip("* ").strip() for line in version_text.splitlines()
         if re.search(r"ngspice-\d", line, re.IGNORECASE)),
        None,
    )
    if version_line is None:
        raise RuntimeError(f"Could not identify ngspice version: {version_text}")

    run_dir = ROOT / "runs" / "smoke"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "rc.log"
    result = subprocess.run(
        [executable, "-b", str(ROOT / "circuits" / "rc.cir")],
        cwd=run_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + "\n" + result.stderr
    log_path.write_text(output, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            f"ngspice batch simulation exited with {result.returncode}. See {log_path}"
        )
    match = re.search(
        r"^\s*gain_at_1khz\s*=\s*([+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)",
        output,
        re.MULTILINE | re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError(f"Missing RC gain measurement. See {log_path}")
    measured = float(match.group(1))
    expected = 1 / math.sqrt(1 + (2 * math.pi * 1000 * 1000 * 1e-6) ** 2)
    if not math.isfinite(measured) or not math.isclose(
        measured, expected, rel_tol=0.01
    ):
        raise RuntimeError(
            f"RC gain {measured} differs from expected {expected} by more than 1%. "
            f"See {log_path}"
        )
    print(f"Python: {sys.version.split()[0]}")
    print(f"ngspice: {version_line}")
    print("Batch simulation: OK")
    print(f"RC gain at 1000 Hz: {measured:.6f} (expected {expected:.6f}, tolerance 1%)")
    print(f"Log: {log_path}")
    print("SMOKE PASS")


def main() -> int:
    try:
        run_smoke()
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(f"SMOKE FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
