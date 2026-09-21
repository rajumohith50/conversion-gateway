"""Upload client, batching, retry, classification. Design sections 7 and 12."""

from gateway.upload.batcher import Batcher
from gateway.upload.classifier import FailureClass, classify_http_status, classify_row_error
from gateway.upload.client import BatchResult, HttpUploadClient, RowResult, UploadClient
from gateway.upload.errors import PermanentUploadError, PoisonUploadError, TransientUploadError

__all__ = [
    "BatchResult",
    "Batcher",
    "FailureClass",
    "HttpUploadClient",
    "PermanentUploadError",
    "PoisonUploadError",
    "RowResult",
    "TransientUploadError",
    "UploadClient",
    "classify_http_status",
    "classify_row_error",
]
