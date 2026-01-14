"""
Optimized Merged Wheel License Extraction and Version Suffix Script

This script extracts license information from .so files in a wheel,
updates the bundled and UBI license files, updates the RECORD, and
then suffixes the wheel version. The wheel is unpacked and packed only once.
"""

import os
import re
import shutil
import subprocess
import tempfile
import sys
import hashlib
import base64
import json
# License extraction utilities
LICENSE_PATTERN = re.compile(r"^(LICENSE|COPYING)(\..*)?$")
LICENSE_SEPARATOR = "----"  # Hardcoded separator for both files

def run_command(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False
    )

def find_libs_dirs(root):
    return [
        os.path.join(dirpath, d)
        for dirpath, dirnames, _ in os.walk(root)
        for d in dirnames
        if d.endswith(".libs")
    ]

def collect_so_files(libs_dir):
    return [
        os.path.join(libs_dir, f)
        for f in os.listdir(libs_dir)
        if f.startswith("lib") and ".so" in f
    ]

def normalize_so_name(so_name):
    return re.sub(r'-[0-9a-f]{8,}(?=(?:\.so|\.\d))', '', so_name)

def find_all_so_anywhere(so_name):
    result = run_command(["find", ".", "-type", "f", "-name", so_name])
    return result.stdout.strip().splitlines()

def get_rpm_package(so_path):
    result = run_command(["rpm", "-qf", so_path])
    if result.returncode == 0:
        return result.stdout.strip()
    return None

def get_rpm_license(pkg_name):
    result = run_command(["rpm", "-q", "--qf", "%{LICENSE}\n", pkg_name])
    if result.returncode == 0:
        return result.stdout.strip()
    return None

def find_project_root(so_path, max_up=10):
    current = os.path.dirname(so_path)
    for _ in range(max_up):
        for f in os.listdir(current):
            if LICENSE_PATTERN.match(f):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None

def find_license_in_directory(directory):
    for f in os.listdir(directory):
        if LICENSE_PATTERN.match(f):
            return os.path.join(directory, f)
    return None

def find_dist_info_dir(root):
    for item in os.listdir(root):
        if item.endswith(".dist-info"):
            return os.path.join(root, item)
    return None

def append_license_entry(file_path, so_names, license_text):
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n\n{LICENSE_SEPARATOR}\n\n\n\n")

    with open(file_path, "a", encoding="utf-8") as f:
        f.write(f"Files: {', '.join(so_names)}\n")
        lines = license_text.strip("\n").splitlines()
        if len(lines) > 1:
            f.write("\n")
            f.write(license_text)
            if not license_text.endswith("\n"):
                f.write("\n")
        else:
            f.write(f"License: {license_text.strip()}\n")

def compute_hash_and_size(file_path):
    with open(file_path, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).digest()
    hash_b64 = base64.urlsafe_b64encode(digest).rstrip(b'=').decode("utf-8")
    size = len(data)
    return f"sha256={hash_b64}", size

def update_record(dist_info_dir, file_paths):
    record_file = os.path.join(dist_info_dir, "RECORD")
    if not os.path.exists(record_file):
        print(f"[WARN] RECORD file not found at {record_file}")
        return

    with open(record_file, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    record_map = {line.split(",")[0]: line.split(",") for line in lines}

    for path in file_paths:
        if not os.path.exists(path):
            continue
        relative_path = os.path.relpath(path, os.path.dirname(dist_info_dir))
        hash_val, size_val = compute_hash_and_size(path)
        record_map[relative_path] = [relative_path, hash_val, str(size_val)]

    with open(record_file, "w", encoding="utf-8", newline="\n") as f:
        for parts in record_map.values():
            f.write(",".join(parts) + "\n")

def process_so_file(so_path, rpm_licenses, bundled_licenses):
    original_name = os.path.basename(so_path)
    normalized_name = normalize_so_name(original_name)

    for match_so in find_all_so_anywhere(normalized_name):
        pkg = get_rpm_package(match_so)
        if pkg:
            license_text = get_rpm_license(pkg)
            if license_text:
                rpm_licenses.setdefault(license_text, []).append(original_name)
            else:
                bundled_licenses.setdefault(f"{original_name}_license_not_found", []).append(original_name)
            return

        project_root = find_project_root(match_so)
        if project_root:
            license_file = find_license_in_directory(project_root)
            if license_file:
                try:
                    with open(license_file, "r", encoding="utf-8", errors="ignore") as f:
                        bundled_licenses.setdefault(f.read(), []).append(original_name)
                    return
                except Exception:
                    pass

    bundled_licenses.setdefault(f"{original_name}_license_not_found", []).append(original_name)

# Wheel version suffix utilities 
def read_version_from_metadata(dist_info_dir):
    metadata_path = os.path.join(dist_info_dir, "METADATA")
    with open(metadata_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError("Version not found in METADATA")

def build_new_version(old_version, suffix):
    if "+" in old_version:
        base, local = old_version.split("+", 1)
        return f"{base}+{local}{suffix}"
    return f"{old_version}+{suffix}"

def update_metadata_version(dist_info_dir, new_version):
    metadata_path = os.path.join(dist_info_dir, "METADATA")
    with open(metadata_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    with open(metadata_path, "w", encoding="utf-8") as f:
        for line in lines:
            if line.startswith("Version:"):
                f.write(f"Version: {new_version}\n")
            else:
                f.write(line)

def rename_dist_info_dir(extract_path, old_version, new_version):
    for entry in os.listdir(extract_path):
        if entry.endswith(".dist-info") and old_version in entry:
            old_path = os.path.join(extract_path, entry)
            new_entry = entry.replace(old_version, new_version)
            new_path = os.path.join(extract_path, new_entry)
            os.rename(old_path, new_path)
            return new_path
    raise RuntimeError("Failed to rename .dist-info directory")

def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return "sha256=" + h.digest().hex()

def regenerate_record(extract_path, dist_info_dir):
    record_path = os.path.join(dist_info_dir, "RECORD")
    records = []

    for root, _, files in os.walk(extract_path):
        for fname in files:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, extract_path)
            rel_path = rel_path.replace(os.sep, "/")

            if rel_path.endswith("RECORD"):
                records.append(f"{rel_path},,")
                continue

            size = os.path.getsize(full_path)
            digest = _hash_file(full_path)
            records.append(f"{rel_path},{digest},{size}")

    with open(record_path, "w", encoding="utf-8") as f:
        f.write("\n".join(records))

def sort_sbom(sbom: dict) -> dict:
    # Sort metadata.tools
    tools = sbom.get("metadata", {}).get("tools")
    if isinstance(tools, list):
        tools.sort(key=lambda x: x.get("name", ""))

    # Sort components by name
    components = sbom.get("components")
    if isinstance(components, list):
        components.sort(key=lambda x: x.get("name", ""))

    # Sort dependencies and their dependsOn lists
    dependencies = sbom.get("dependencies")
    if isinstance(dependencies, list):
        dependencies.sort(key=lambda x: x.get("ref", ""))
        for dep in dependencies:
            if isinstance(dep.get("dependsOn"), list):
                dep["dependsOn"].sort()

    return sbom


def sort_sbom_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        sbom = json.load(f)

    sbom = sort_sbom(sbom)

    dir_name = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=dir_name,
        delete=False
    ) as tmp:
        json.dump(sbom, tmp, indent=2, sort_keys=False)
        tmp.write("\n")

    os.replace(tmp.name, path)


# Main processing function
def process_wheel(wheel_path, suffix):
    wheel_dir = os.path.dirname(wheel_path)
    wheel_name = os.path.basename(wheel_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Unpack wheel
        subprocess.run(["wheel", "unpack", wheel_path, "-d", tmpdir], check=True)
        dirs = [d for d in os.listdir(tmpdir) if os.path.isdir(os.path.join(tmpdir, d))]
        if len(dirs) != 1:
            raise RuntimeError(f"Unexpected unpack layout. Found directories: {dirs}")
        extract_path = os.path.join(tmpdir, dirs[0])

        # License processing
        rpm_licenses = {}
        bundled_licenses = {}

        libs_dirs = find_libs_dirs(extract_path)
        if libs_dirs:
            for libs_dir in libs_dirs:
                so_files = collect_so_files(libs_dir)
                for so_file in so_files:
                    process_so_file(so_file, rpm_licenses, bundled_licenses)

        dist_info = find_dist_info_dir(extract_path)
        if dist_info:
            ubi_path = os.path.join(dist_info, "UBI_BUNDLED_LICENSES.txt")
            bundled_path = os.path.join(dist_info, "BUNDLED_LICENSES.txt")

            for license_text, files in rpm_licenses.items():
                append_license_entry(ubi_path, files, license_text)
            for license_text, files in bundled_licenses.items():
                append_license_entry(bundled_path, files, license_text)

            existing_license_files = [p for p in [ubi_path, bundled_path] if os.path.exists(p)]
            if existing_license_files:
                update_record(dist_info, existing_license_files)

            # Version suffix processing
            old_version = read_version_from_metadata(dist_info)
            new_version = build_new_version(old_version, suffix)
            update_metadata_version(dist_info, new_version)
            dist_info = rename_dist_info_dir(extract_path, old_version, new_version)
            regenerate_record(extract_path, dist_info)
            # Sort SBOM after version suffix
            for root, _, files in os.walk(extract_path):
                if root.endswith(os.path.join(".dist-info", "sboms")):
                    for name in files:
                        if name.lower().endswith(".json"):
                            sort_sbom_file(os.path.join(root, name))
            # SBOM changed → regenerate RECORD again
            regenerate_record(extract_path, dist_info)
        # Pack wheel
        subprocess.run(["wheel", "pack", extract_path, "-d", wheel_dir], check=True)

    new_wheel_name = wheel_name
    if "+" in old_version:
        base, local = old_version.split("+", 1)
        new_wheel_name = wheel_name.replace(f"{base}+{local}", f"{base}+{local}{suffix}", 1)
    else:
        new_wheel_name = wheel_name.replace(old_version, f"{old_version}+{suffix}", 1)

    new_wheel_path = os.path.join(wheel_dir, new_wheel_name)
    os.remove(wheel_path)
    return new_wheel_path

def main():
    if len(sys.argv) != 3:
        print("Usage: python merged_wheel_script.py <wheel_file.whl> <suffix>")
        sys.exit(1)

    wheel_path = sys.argv[1]
    suffix = sys.argv[2]
    new_wheel = process_wheel(wheel_path, suffix)
    print(f"[INFO] Wheel updated: {new_wheel}")

if __name__ == "__main__":
    main()