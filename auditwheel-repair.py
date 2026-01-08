#!/usr/bin/env python3

"""
Prequistes:
1. Python Version
    - Python 3.8 or higher
2. Required Python Packages
    - requests
    - auditwheel
3. Artifactory Access
    - Update the following variables with valid values:
       ARTIFACTORY_URL = ''
       ARTIFACTORY_API_KEY = ''
       ARTIFACTORY_REPOSITORY = ''

Usage:
    python auditwheel-repair.py

Description:
This script automates the process of repairing Python wheels to ensure compatibility with different Python versions and platforms.It performs the following steps:
1. Fetch wheel metadata from Artifactory.
2. Download wheels to a local directory.
3. Run `auditwheel repair` on each wheel.
4. Upload the successfully repaired wheels back to Artifactory.
5. Test the repaired wheels by attempting to install them in isolated virtual environments.
6. Generate summary reports of the repair process.
Environment Variables:
The script uses several configuration variables defined in the `config.py` file.
"""


import os
import csv
import zipfile
import shutil
import subprocess
import requests
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import *

HEADERS = {"X-JFrog-Art-Api": JFROG_API_KEY}

#Summary of entire run
SUMMARY = {
    "total": 0,
    "audit_success": 0,
    "audit_failed": 0,
    "pip_success": 0,
    "pip_failed": 0,
    "pip_skipped": 0,
    "no_elf": 0,
    "native_repaired": 0,
    "already_processed": 0,
    "newly_processed": 0,
}

# Helper to run a command and capture output
def run(cmd):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


# Python binary map for different python versions
PYTHON_BIN_MAP = {
    "cp39": "/opt/python/cp39-cp39/bin/python",
    "cp310": "/opt/python/cp310-cp310/bin/python",
    "cp311": "/opt/python/cp311-cp311/bin/python",
    "cp312": "/opt/python/cp312-cp312/bin/python",
    "cp313": "/opt/python/cp313-cp313/bin/python",
}


def extract_python_tag(name):
    """Extract the python tag from a wheel filename."""
    for p in name.split("-"):
        if p.startswith("cp") and p[2:].isdigit():
            return p
    return None


def create_venv(py):
    """Create a virtual environment using the specified python binary."""
    d = tempfile.mkdtemp(prefix="wheel-venv-")
    subprocess.check_call([py, "-m", "venv", d])
    return d


def upgrade_pip(venv):
    """Upgrade pip, setuptools, wheel in the specified venv."""
    py = os.path.join(venv, "bin", "python")
    subprocess.check_call(
        [py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"]
    )


def pip_install(venv, wheel):
    """Install the specified wheel into the specified venv."""
    py = os.path.join(venv, "bin", "python")
    return subprocess.run(
        [py, "-m", "pip", "install", "--no-deps", wheel],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def upload(wheel, pkg, ver):
    """Upload the specified wheel to Artifactory."""
    path = f"{UPLOAD_ROOT_FOLDER}/{pkg}/{ver}/{os.path.basename(wheel)}"
    url = f"{ART_URL}/{TARGET_UPLOAD_REPO}/{path}"
    with open(wheel, "rb") as f:
        r = requests.put(url, headers=HEADERS, data=f)
    if r.status_code not in (200, 201):
        raise RuntimeError(r.text)


def fetch_wheels():
    """Fetch wheel metadata from Artifactory."""
    query = (
        "items.find({"
        f'"repo": "{SOURCE_REPO}",'
        '"name": {"$match": "*.whl"},'
        '"path": {"$nmatch": ".pypi*"}'
        '}).include("repo","path","name")'
    )

    r = requests.post(JFROG_AQL_URL, headers=HEADERS, data=query)
    r.raise_for_status()

    wheels = r.json().get("results", [])
    return wheels[:MAX_TOTAL_WHEELS] if MAX_TOTAL_WHEELS else wheels


def download(item):
    """Download the specified wheel from Artifactory."""
    pkg, ver = item["name"].split("-")[0:2]
    d = os.path.join(DOWNLOAD_DIR, pkg, ver)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, item["name"])
    if not os.path.exists(p):
        url = f"{ART_URL}/{item['repo']}/{item['path']}/{item['name']}"
        with requests.get(url, headers=HEADERS, stream=True) as r:
            r.raise_for_status()
            with open(p, "wb") as f:
                for c in r.iter_content(1024 * 1024):
                    if c:
                        f.write(c)
    return p


def auditwheel_repair(wheel, pkg, ver):
    """Run auditwheel repair on the specified wheel."""
    out = os.path.join(REPAIRED_DIR, pkg, ver)
    os.makedirs(out, exist_ok=True)
    """ Set LD_LIBRARY_PATH to ensure auditwheel can find system libs """
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = "/usr/local/lib64:/usr/local/lib"

    return (
        subprocess.run(
            [
                "auditwheel",
                "repair",
                "--plat",
                "manylinux_2_34_ppc64le",
                "--only-plat",
                wheel,
                "-w",
                out,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        ),
        out,
    )


def process_wheel(item):
    """Process a single wheel: download, auditwheel repair, pip install, upload."""
    SUMMARY["total"] += 1

    wheel = download(item)
    name = os.path.basename(wheel)
    print("DEBUG name =", repr(name))
    pkg, ver = name.split("-")[0:2]

    py_tag = extract_python_tag(name)

    if py_tag is None:
        for tag in ["cp313", "cp312", "cp311", "cp310", "cp39"]:
            if tag in PYTHON_BIN_MAP:
                py_tag = tag
                break

    if "abi3" in name and py_tag not in PYTHON_BIN_MAP:
        py_tag = "cp311"

    audit_status = "SUCCESS"
    audit_msg = ""
    pip_status = "SKIPPED"
    pip_msg = ""

    bundled_libs = []
    wheel_to_test = wheel

    # --------------------------------------------------
    # NO-ARCH wheels: py3-none-any / py2.py3-none-any
    # --------------------------------------------------
    if name.endswith("-py3-none-any.whl") or name.endswith("-py2.py3-none-any.whl"):
        audit_status = "SUCCESS"
        audit_msg = "no-arch wheel (auditwheel skipped)"
        pip_status = "SKIPPED"
        pip_msg = ""
        bundled_libs = []

        SUMMARY["no_elf"] += 1
        SUMMARY["audit_success"] += 1

        # Upload original wheel directly
        upload(wheel, pkg, ver)

        # ---- pip test MUST still happen ----
        if py_tag in PYTHON_BIN_MAP:
            venv = create_venv(PYTHON_BIN_MAP[py_tag])
            try:
                upgrade_pip(venv)
                r = pip_install(venv, wheel)
                if r.returncode == 0:
                    pip_status = "SUCCESS"
                    SUMMARY["pip_success"] += 1
                else:
                    pip_status = "FAILED"
                    pip_msg = r.stderr.strip().splitlines()[-1]
                    SUMMARY["pip_failed"] += 1
            finally:
                shutil.rmtree(venv, ignore_errors=True)

        # Cleanup
        try:
            if os.path.exists(wheel):
                os.remove(wheel)
        except Exception:
            pass

        return name, audit_status, audit_msg, pip_status, pip_msg, bundled_libs

    # --------------------------------------------------
    # auditwheel repair for native wheels
    # --------------------------------------------------
    res, out = auditwheel_repair(wheel, pkg, ver)

    # ---------------------------
    # auditwheel FAILED
    # ---------------------------
    if res.returncode != 0:
        err = res.stderr.lower()

        # True no-arch wheel (py3-none-any should never reach here anyway)
        if "no elf" in err:
            audit_status = "SUCCESS"
            audit_msg = "no ELF files found (no-arch wheel)"
            SUMMARY["no_elf"] += 1
            SUMMARY["audit_success"] += 1
        else:
            audit_status = "FAILED"
            audit_msg = res.stderr.strip().splitlines()[-1]
            SUMMARY["audit_failed"] += 1
            SUMMARY["pip_skipped"] += 1
            return name, audit_status, audit_msg, "SKIPPED", "", []

    # ---------------------------
    # auditwheel SUCCEEDED
    # ---------------------------
    repaired_wheels = [
        os.path.join(out, f)
        for f in os.listdir(out)
        if f.endswith(".whl")
    ]

    if not repaired_wheels:
        # ❗ THIS is the missing enforcement
        audit_status = "FAILED"
        audit_msg = "auditwheel succeeded but produced no manylinux wheel"
        SUMMARY["audit_failed"] += 1
        SUMMARY["pip_skipped"] += 1
        return name, audit_status, audit_msg, "SKIPPED", "", []

    # ---------------------------
    # Valid manylinux wheel
    # ---------------------------
    wheel_to_test = repaired_wheels[0]
    SUMMARY["native_repaired"] += 1

    with zipfile.ZipFile(wheel_to_test) as z:
        bundled_libs = [n for n in z.namelist() if n.endswith(".so")]

    audit_status = "SUCCESS"
    SUMMARY["audit_success"] += 1


    # --------------------------------------------------
    # Test pip install
    # --------------------------------------------------
    if py_tag in PYTHON_BIN_MAP:
        venv = create_venv(PYTHON_BIN_MAP[py_tag])
        try:
            upgrade_pip(venv)
            r = pip_install(venv, wheel_to_test)
            if r.returncode == 0:
                pip_status = "SUCCESS"
                SUMMARY["pip_success"] += 1
                upload(wheel_to_test, pkg, ver)
            else:
                pip_status = "FAILED"
                pip_msg = r.stderr.strip().splitlines()[-1]
                SUMMARY["pip_failed"] += 1
        finally:
            shutil.rmtree(venv, ignore_errors=True)

    # --------------------------------------------------
    # Cleanup of temporary files
    # --------------------------------------------------
    for f in {wheel, wheel_to_test}:
        try:
            if f and os.path.exists(f):
                os.remove(f)
        except Exception:
            pass

    return name, audit_status, audit_msg, pip_status, pip_msg, bundled_libs



def load_already_successful_wheels():
    """Load already successfully processed wheels from the status CSV."""
    done = set()
    path = os.path.join(OUTPUT_DIR, "wheel_status.csv")
    if not os.path.exists(path):
        return done

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            done.add(row["wheel_path"])

    return done


def main():
    """Main processing function."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    existing_status = {}
    status_csv = os.path.join(OUTPUT_DIR, "wheel_status.csv")

    if os.path.exists(status_csv):
        with open(status_csv, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_status[row["wheel_path"]] = row

    already_processed_count = len(existing_status)
    for row in existing_status.values():
        if row["auditwheel_status"] == "SUCCESS":
            SUMMARY["audit_success"] += 1
        elif row["auditwheel_status"] == "FAILED":
            SUMMARY["audit_failed"] += 1

        if row["pip_install_status"] == "SUCCESS":
            SUMMARY["pip_success"] += 1
        elif row["pip_install_status"] == "FAILED":
            SUMMARY["pip_failed"] += 1
        elif row["pip_install_status"] == "SKIPPED":
            SUMMARY["pip_skipped"] += 1

        if "no elf" in (row["auditwheel_message"] or "").lower():
            SUMMARY["no_elf"] += 1

    existing_all = {}
    existing_ext = {}

    all_csv = os.path.join(OUTPUT_DIR, "native_libs_all.csv")
    ext_csv = os.path.join(OUTPUT_DIR, "native_libs_external.csv")

    if os.path.exists(all_csv):
        with open(all_csv, newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                existing_all.setdefault(row["wheel_path"], []).append(
                    row["native_library"]
                )

    if os.path.exists(ext_csv):
        with open(ext_csv, newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                existing_ext.setdefault(row["wheel_path"], []).append(
                    row["native_library"]
                )

    for wheel, libs in existing_all.items():
        if any(lib != "not found" for lib in libs):
            SUMMARY["native_repaired"] += 1

    wheels = fetch_wheels()
    wheels = sorted(wheels, key=lambda w: w["name"])

    to_process = [w for w in wheels if w["name"] not in existing_status]

    print("TOTAL FETCHED:", len(wheels))
    print("ALREADY PROCESSED (from CSV):", already_processed_count)
    print("TO PROCESS:", len(to_process))

    SUMMARY["already_processed"] = already_processed_count
    SUMMARY["newly_processed"] = len(to_process)
    overall_total = SUMMARY["already_processed"] + SUMMARY["newly_processed"]

    processed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process_wheel, w) for w in to_process]

        for f in as_completed(futures):
            result = f.result()
            processed += 1

            processed_so_far = SUMMARY["already_processed"] + processed
            remaining = overall_total - processed_so_far

            print(
                f"[PROGRESS] {processed_so_far}/{overall_total} processed | remaining: {remaining}",
                flush=True,
            )

            name, a_s, a_m, p_s, p_m, bundled = result

            existing_status[name] = {
                "wheel_path": name,
                "auditwheel_status": a_s,
                "auditwheel_message": a_m,
                "pip_install_status": p_s,
                "pip_install_message": p_m,
            }

            existing_all[name] = []
            existing_ext[name] = []

            if bundled:
                for so in bundled:
                    existing_all[name].append(so)
                    existing_ext[name].append(so)
            else:
                existing_all[name].append("not found")
                existing_ext[name].append("not found")

            with open(status_csv, "w", newline="") as fw:
                w = csv.writer(fw)
                w.writerow(
                    [
                        "wheel_path",
                        "auditwheel_status",
                        "auditwheel_message",
                        "pip_install_status",
                        "pip_install_message",
                    ]
                )
                for row in existing_status.values():
                    w.writerow(
                        [
                            row["wheel_path"],
                            row["auditwheel_status"],
                            row["auditwheel_message"],
                            row["pip_install_status"],
                            row["pip_install_message"],
                        ]
                    )

            """ Update all libs CSVs """
            with open(all_csv, "w", newline="") as fw:
                w = csv.writer(fw)
                w.writerow(["wheel_path", "native_library"])
                for wheel, libs in existing_all.items():
                    for lib in libs:
                        w.writerow([wheel, lib])
            """ Update bundled libs CSVs """
            with open(ext_csv, "w", newline="") as fw:
                w = csv.writer(fw)
                w.writerow(["wheel_path", "native_library"])
                for wheel, libs in existing_ext.items():
                    for lib in libs:
                        w.writerow([wheel, lib])

    print("\n===== SUMMARY =====")
    print(f"overall_total: {overall_total}")
    for k, v in SUMMARY.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
