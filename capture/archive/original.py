from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class ArchivedOriginal:
    raw: bytes
    sha256: str


def capture_original(raw: bytes) -> ArchivedOriginal:
    if not isinstance(raw, bytes):
        raise TypeError('original export must be bytes')
    data = bytes(raw)
    return ArchivedOriginal(data, sha256(data).hexdigest())


def verify_original_digest(original: ArchivedOriginal) -> bool:
    return sha256(original.raw).hexdigest() == original.sha256
