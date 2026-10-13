"""File helpers shared by firmware builds and device tools."""
import hashlib


def file_sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()
