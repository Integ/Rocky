"""Stable identity for one verified set of exported artifacts; no I/O."""

import hashlib
import json


def archive_identity(artifacts) -> str:
    data = sorted(
        (item.model_dump(mode="json") for item in artifacts), key=lambda item: item["path"]
    )
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
