"""Document text extraction for the estimator.

Spec: §19.5.3.

The implementation lives in `extraction.py`, not `extract.py`: this package
re-exports a function named `extract`, which would shadow a submodule of the
same name and make `gyrfalcon.documents.extract` mean two different objects
depending on import order.
"""

from gyrfalcon.documents.extraction import (
    MAX_BYTES,
    MAX_PAGES,
    Extracted,
    Upload,
    extract,
    get_upload,
    purge_expired,
    store_upload,
)

__all__ = [
    "MAX_BYTES",
    "MAX_PAGES",
    "Extracted",
    "Upload",
    "extract",
    "get_upload",
    "purge_expired",
    "store_upload",
]
