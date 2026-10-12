"""Proxy Attest storage without handing cloud credentials to merge agents."""
import json
import os
from pathlib import Path
import subprocess


def request(payload):
    binary = os.environ.get("ATTEST_BIN", str(Path.home() / ".local/bin/attest"))
    config = os.environ.get("ATTEST_STORAGE_CONFIG", str(Path.home() / ".config/attest/storage.toml"))
    result = subprocess.run([binary, "store", "--config", config], input=json.dumps(payload),
                            text=True, capture_output=True, timeout=25)
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("Attest storage adapter returned no valid response") from None
    if result.returncode or response.get("error"):
        raise RuntimeError(response.get("error", "Attest storage request failed"))
    return response
