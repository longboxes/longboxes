"""Filesystem scanner.

Public API:
    ``scan_all_libraries()`` — async coroutine; walks every configured
                                library and reconciles ``files`` /
                                ``file_locations`` per §9 v0.5.
    ``ScanResult``           — counters returned from a scan (new content,
                                moved, duplicated, missing, errors, etc.).

The scanner is normally invoked from the ``scan_all_libraries`` RQ job
(see ``app.jobs.scan``), but is also directly callable from tests.
"""

from app.scanner.scanner import ScanResult, scan_all_libraries

__all__ = ["ScanResult", "scan_all_libraries"]
