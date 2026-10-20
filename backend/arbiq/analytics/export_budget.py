"""Hard publication budget, deliberately expressed in decimal bytes."""

import zipfile

MAX_ZIP_BYTES = 512_000_000
TARGET_ZIP_BYTES = 300_000_000
MAX_TEMP_BYTES = 2_000_000_000


class ExportBudgetExceeded(RuntimeError):
    pass


class ExportCancelled(RuntimeError):
    pass


def verify_archive(path):
    """Verify the closed file, including the central directory, before publication."""
    size = path.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise ExportBudgetExceeded("ZIP_BUDGET: completed archive exceeds512000000bytes")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP_INTEGRITY: corrupt member; archive not published")
    return size
