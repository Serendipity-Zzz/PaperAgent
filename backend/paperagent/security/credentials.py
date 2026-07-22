from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(b"PaperAgent:v1")
    output = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        "PaperAgent credential",
        ctypes.byref(entropy),
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer, entropy_buffer


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(b"PaperAgent:v1")
    output = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer, entropy_buffer


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def put(self, provider_id: str, value: str, reference: str | None = None) -> str:
        if not value:
            raise ValueError("Credential must not be empty")
        ref = reference or f"{provider_id}:{uuid4()}"
        records = self._load()
        records[ref] = base64.b64encode(protect(value.encode())).decode()
        self._save(records)
        return ref

    def get(self, reference: str) -> str:
        records = self._load()
        try:
            encrypted = base64.b64decode(records[reference], validate=True)
            return unprotect(encrypted).decode()
        except KeyError as error:
            raise KeyError("Credential reference not found") from error

    def has(self, reference: str | None) -> bool:
        return bool(reference and reference in self._load())

    def status(self, reference: str | None) -> str:
        if not reference:
            return "missing"
        return "available" if self.has(reference) else "unavailable"

    def delete(self, reference: str) -> bool:
        records = self._load()
        existed = records.pop(reference, None) is not None
        if existed:
            self._save(records)
        return existed

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise ValueError("Credential store is corrupt")
        return data

    def _save(self, records: dict[str, str]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
