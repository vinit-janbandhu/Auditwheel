# ---------------- ARTIFACTORY ----------------
ART_URL = "https://na.artifactory.swg-devops.com/artifactory"
JFROG_AQL_URL = "https://na.artifactory.swg-devops.com/artifactory/api/search/aql"

JFROG_API_KEY = ""

SOURCE_REPO = "sys-linux-power-team-pyeco-main-pypi-local"
TARGET_UPLOAD_REPO = "sys-linux-power-team-pyeco-auditwheel-testing-pypi-local"
UPLOAD_ROOT_FOLDER = "auditwheel-repair"

# ---------------- DIRECTORIES ----------------
DOWNLOAD_DIR = "wheels"
REPAIRED_DIR = "repaired_wheels"
EXTRACT_DIR = "extracted"
OUTPUT_DIR = "output"


# ---------------- BASE SYSTEM LIBS ----------------
BASE_SYSTEM_LIBS = (
    "libc.so",
    "libm.so",
    "libstdc++.so",
    "libgcc_s.so",
    "libpthread.so",
    "libdl.so",
    "librt.so",
    "linux-vdso",
)

# ---------------- LIMITS ----------------
MAX_TOTAL_WHEELS = 0   # 0 means no limit
MAX_WORKERS = 4

REPROCESS_FAILED_PACKAGES = ["numpy"]

REPROCESS_ALL = False
