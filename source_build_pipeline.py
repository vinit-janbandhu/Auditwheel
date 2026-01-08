import os
import csv
import subprocess
import tempfile
from collections import defaultdict

# ---------------- CONFIG ----------------
CSV_PATH = "output/wheel_status.csv"
BUILD_SCRIPTS_REPO = "https://github.com/ppc64le/build-scripts.git"
BUILD_SCRIPTS_DIR = os.path.join(tempfile.gettempdir(), "build-scripts")
BUILD_WHEELS_SCRIPT = os.path.join(BUILD_SCRIPTS_DIR, "gha-script", "build_wheels.py")


# ---------------- HELPERS ----------------
def find_script(root_dir, script_name):
    for root, _, files in os.walk(root_dir):
        if script_name in files:
            return os.path.join(root, script_name)
    return None


def python_version_from_wheel(wheel_name):
    parts = wheel_name.split("-")
    for p in parts:
        if p.startswith("cp") and p[2:].isdigit():
            tag = p[2:]
            if len(tag) == 2:
                return f"{tag[0]}.{tag[1]}"
            if len(tag) == 3:
                return f"{tag[0]}.{tag[1:]}"
    return None


def run(cmd, cwd=None, env=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def main():
    # --------------------------------------------------
    # 1. Load FAILED wheels from CSV
    # --------------------------------------------------
    if not os.path.exists(CSV_PATH):
        print("[ERROR] CSV not found:", CSV_PATH)
        return

    failed_wheels = []

    with open(CSV_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("auditwheel_status") == "FAILED":
                failed_wheels.append(row["wheel_path"])

    print("\n===== PHASE 2 : SOURCE BUILD PIPELINE =====")
    print(f"Total FAILED wheels found: {len(failed_wheels)}")

    if not failed_wheels:
        print("Nothing to process. Exiting.")
        return

    # --------------------------------------------------
    # 2. clone build-scripts repo 
    # --------------------------------------------------
    print("\n[SETUP] Ensuring build-scripts repository")

    if not os.path.exists(BUILD_SCRIPTS_DIR):
        print("[SETUP] Cloning build-scripts repo...")
        r = run(["git", "clone", BUILD_SCRIPTS_REPO, BUILD_SCRIPTS_DIR])
        if r.returncode != 0:
            print("[ERROR] Failed to clone build-scripts repo")
            print(r.stderr)
            return
        print("[SETUP] build-scripts cloned successfully")
    else:
        print("[SETUP] Using existing repo:", BUILD_SCRIPTS_DIR)

    # --------------------------------------------------
    # 3. Locate required scripts
    # --------------------------------------------------
    read_buildinfo = find_script(BUILD_SCRIPTS_DIR, "read_buildinfo.sh")
    create_wheel = find_script(BUILD_SCRIPTS_DIR, "create_wheel_wrapper.sh")

    if not read_buildinfo or not create_wheel:
        print("[ERROR] Required scripts not found")
        print("read_buildinfo.sh:", read_buildinfo)
        print("create_wheel_wrapper.sh:", create_wheel)
        return

    # --------------------------------------------------
    # 4. Process wheels ONE BY ONE
    # --------------------------------------------------
    total = len(failed_wheels)

    for idx, wheel_name in enumerate(failed_wheels, start=1):
        print("\n" + "=" * 80)
        print(f"[{idx}/{total}] Processing wheel: {wheel_name}")
        print("=" * 80)

        parts = wheel_name.split("-")
        pkg_name = parts[0]
        version = parts[1]
        py_version = python_version_from_wheel(wheel_name)

        print(f"[INFO] Package       : {pkg_name}")
        print(f"[INFO] Version       : {version}")
        print(f"[INFO] Python version: {py_version}")

        if not py_version:
            print("[SKIP] Could not determine python version")
            continue

        # --------------------------------------------------
        # 5. Resolve BUILD_SCRIPT and IMAGE_NAME
        # --------------------------------------------------
        env = os.environ.copy()
        env["PACKAGE_NAME"] = pkg_name
        env["VERSION"] = version

        print("[STEP] Resolving build metadata (read_buildinfo.sh)")
        r = run(["bash", read_buildinfo], cwd=BUILD_SCRIPTS_DIR, env=env)

        build_script = run(
        ["bash", "-c", "source variable.sh && echo $BUILD_SCRIPT"],
        cwd=BUILD_SCRIPTS_DIR,
        ).stdout.strip()


        image_name = run(
        ["bash", "-c", "source variable.sh && echo $IMAGE_NAME"],
        cwd=BUILD_SCRIPTS_DIR,
        ).stdout.strip()

        if build_script and "/" not in build_script:
            build_script = f"scripts/{build_script}"


        # --------------------------------------------------
        # 6. Fallback version if BUILD_SCRIPT empty
        # --------------------------------------------------
        if not build_script:
            print("[INFO] BUILD_SCRIPT empty, trying fallback version")
            try:
                major, minor, *_ = version.split(".")
                fallback_version = f"{major}.{minor}.0"
            except Exception:
                print("[SKIP] Invalid version format")
                continue

            env["VERSION"] = fallback_version
            print("[INFO] Retrying with VERSION =", fallback_version)

            run(["bash", read_buildinfo], cwd=BUILD_SCRIPTS_DIR, env=env)

            build_script = run(
            ["bash", "-c", "source variable.sh && echo $BUILD_SCRIPT"],
            cwd=BUILD_SCRIPTS_DIR,
            ).stdout.strip()


            image_name = run(
            ["bash", "-c", "source variable.sh && echo $IMAGE_NAME"],
            cwd=BUILD_SCRIPTS_DIR,
            ).stdout.strip()

            if not build_script:
                print("[SKIP] No BUILD_SCRIPT even after fallback")
                continue

        print("[INFO] BUILD_SCRIPT :", build_script)
        print("[INFO] IMAGE_NAME  :", image_name)

        # --------------------------------------------------
        # 6.5 create docker image if not exists
        # --------------------------------------------------
        print("[STEP] Ensuring docker image exists")

        img_check = run(
            ["docker", "image", "inspect", image_name],
        )

        if img_check.returncode != 0:
            print(f"[INFO] Image not found locally, creating base image: {image_name}")

            r = run(
                [
                    "docker", "run", "--name", "tmp-build-image",
                    "registry.access.redhat.com/ubi9/ubi", "true"
                ]
            )

            if r.returncode != 0:
                print("[FAIL] Unable to start base container")
                continue

            run(["docker", "commit", "tmp-build-image", image_name])
            run(["docker", "rm", "tmp-build-image"])

            print(f"[INFO] Docker image created: {image_name}")
        else:
            print("[INFO] Docker image already exists")

        # --------------------------------------------------
        # 7. Build wheel inside container
        # --------------------------------------------------
        print("[STEP] Building wheel in container")

        wrapper_name = os.path.basename(create_wheel)

        r = run(
            [
                "python",
                BUILD_WHEELS_SCRIPT,
                wrapper_name,
                py_version,
                image_name,
                build_script,
                version,
            ],
            cwd=os.path.dirname(BUILD_WHEELS_SCRIPT),
        )


        print("----- BUILD STDOUT -----")
        print(r.stdout)
        print("----- BUILD STDERR -----")
        print(r.stderr)

        if r.returncode != 0:
            print("[FAIL] Source build failed")
            continue

        print("[SUCCESS] Source build completed")

    print("\n===== PHASE 2 COMPLETED =====")


if __name__ == "__main__":
    main()
