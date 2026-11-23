"""Manifest-driven FIA regulation PDF fetcher and SHA-256 provenance verifier (Phase P1)."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

DEFAULT_MANIFEST_PATH = Path("corpus/manifest.json")
DEFAULT_RAW_DIR = Path("data/raw")

_CHUNK_SIZE = 65_536
_PDF_MAGIC = b"%PDF-"
_USER_AGENT = "regrag/0.1.0 (+https://github.com/regrag; FIA regulation corpus fetcher)"
_ALLOWED_DOC_TYPES = frozenset({"technical", "sporting"})
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_YEAR = 1950
_MAX_YEAR = 2100

_REQUIRED_DOC_FIELDS = (
    "doc_id",
    "year",
    "doc_type",
    "issue",
    "published_date",
    "title",
    "filename",
    "url",
    "sha256",
    "expected_bytes",
    "expected_pages",
)


class ManifestError(ValueError):
    """Raised when a corpus manifest is missing, malformed, or violates schema rules."""


class ChecksumMismatchError(RuntimeError):
    """Raised when a downloaded artifact's SHA-256 digest does not match the manifest."""


class FetchError(RuntimeError):
    """Raised when downloading a regulation PDF fails after retries or returns invalid content."""


class _HttpResponse(Protocol):
    """Minimal streaming HTTP response protocol compatible with requests.Response."""

    status_code: int

    def iter_content(self, chunk_size: int = ...) -> Iterator[bytes]: ...
    def close(self) -> None: ...
    def __enter__(self) -> _HttpResponse: ...
    def __exit__(self, *args: object) -> object: ...


class _HttpSession(Protocol):
    """Minimal HTTP session protocol compatible with requests.Session."""

    def get(
        self,
        url: str,
        *,
        stream: bool = ...,
        timeout: float = ...,
        headers: Mapping[str, str] | None = ...,
    ) -> _HttpResponse: ...


@dataclass(frozen=True)
class ManifestDocument:
    """Immutable metadata and provenance entry for one regulation PDF."""

    doc_id: str
    year: int
    doc_type: str
    issue: str
    published_date: str
    title: str
    filename: str
    url: str
    sha256: str
    expected_bytes: int
    expected_pages: int


@dataclass(frozen=True)
class CorpusManifest:
    """Immutable corpus manifest describing all tracked regulation documents."""

    manifest_version: int
    corpus_name: str
    documents: tuple[ManifestDocument, ...]


@dataclass(frozen=True)
class FetchResult:
    """Outcome of verifying or downloading a single corpus document."""

    doc_id: str
    path: Path
    status: str
    sha256: str
    size_bytes: int
    attempts: int


def _is_strict_int(value: Any) -> bool:
    """Return True iff value is an int and not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_non_empty_str(entry: Mapping[str, Any], field: str, context: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{context}: field '{field}' must be a non-empty string")
    return value.strip()


def _validate_filename(filename: Any, context: str) -> str:
    """Validate that filename is a safe single PDF filename with no path traversal."""
    if not isinstance(filename, str) or not filename:
        raise ManifestError(f"{context}: field 'filename' must be a non-empty string")
    if (
        "/" in filename
        or "\\" in filename
        or ":" in filename
        or ".." in filename
        or "\x00" in filename
        or Path(filename).name != filename
        or filename == ".pdf"
        or not filename.endswith(".pdf")
    ):
        raise ManifestError(
            f"{context}: unsafe or non-PDF 'filename' {filename!r} "
            "(must be a single safe *.pdf filename)"
        )
    return filename


def _validate_https_url(url: Any, context: str) -> str:
    """Validate that url uses https:// with a non-empty host."""
    if not isinstance(url, str) or not url.strip():
        raise ManifestError(f"{context}: field 'url' must be a non-empty https:// URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ManifestError(f"{context}: field 'url' must be a valid https:// URL, got {url!r}")
    return url


def _validate_sha256(sha256: Any, context: str) -> str:
    """Validate and normalize a 64-character hexadecimal SHA-256 digest."""
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ManifestError(f"{context}: field 'sha256' must be a 64-character hex digest")
    return sha256.lower()


def _validate_iso_date(published_date: Any, context: str) -> str:
    """Validate an ISO-8601 calendar date (YYYY-MM-DD)."""
    if not isinstance(published_date, str) or not _ISO_DATE_RE.fullmatch(published_date):
        raise ManifestError(f"{context}: field 'published_date' must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(published_date)
    except ValueError as exc:
        raise ManifestError(
            f"{context}: field 'published_date' is not a valid calendar date: {published_date!r}"
        ) from exc
    return published_date


def _parse_document(raw: Any, index: int) -> ManifestDocument:
    """Validate and construct a ManifestDocument from raw JSON data."""
    context = f"documents[{index}]"
    if not isinstance(raw, Mapping):
        raise ManifestError(f"{context}: document entry must be a JSON object")

    missing = [field for field in _REQUIRED_DOC_FIELDS if field not in raw]
    if missing:
        raise ManifestError(f"{context}: missing required field(s): {', '.join(missing)}")

    doc_id = _require_non_empty_str(raw, "doc_id", context)
    year = raw["year"]
    if not _is_strict_int(year) or not (_MIN_YEAR <= year <= _MAX_YEAR):
        raise ManifestError(
            f"{context}: field 'year' must be an integer between {_MIN_YEAR} and {_MAX_YEAR}"
        )

    doc_type = raw["doc_type"]
    if not isinstance(doc_type, str) or doc_type not in _ALLOWED_DOC_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_DOC_TYPES))
        raise ManifestError(f"{context}: field 'doc_type' must be one of {{{allowed}}}")

    issue = _require_non_empty_str(raw, "issue", context)
    published_date = _validate_iso_date(raw["published_date"], context)
    title = _require_non_empty_str(raw, "title", context)
    filename = _validate_filename(raw["filename"], context)
    url = _validate_https_url(raw["url"], context)
    sha256 = _validate_sha256(raw["sha256"], context)

    expected_bytes = raw["expected_bytes"]
    if not _is_strict_int(expected_bytes) or expected_bytes <= 0:
        raise ManifestError(f"{context}: field 'expected_bytes' must be a positive integer")

    expected_pages = raw["expected_pages"]
    if not _is_strict_int(expected_pages) or expected_pages <= 0:
        raise ManifestError(f"{context}: field 'expected_pages' must be a positive integer")

    return ManifestDocument(
        doc_id=doc_id,
        year=year,
        doc_type=doc_type,
        issue=issue,
        published_date=published_date,
        title=title,
        filename=filename,
        url=url,
        sha256=sha256,
        expected_bytes=expected_bytes,
        expected_pages=expected_pages,
    )


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> CorpusManifest:
    """Load and strictly validate a corpus manifest JSON file.

    Args:
        path: Path to the manifest JSON file.

    Returns:
        Validated immutable CorpusManifest instance.

    Raises:
        ManifestError: If the file cannot be read, is invalid JSON, or violates schema constraints.
    """
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Manifest file could not be read ({path}): {exc}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Invalid JSON in manifest ({path}): {exc}") from exc

    if not isinstance(payload, dict):
        raise ManifestError("Manifest root must be a JSON object")

    manifest_version = payload.get("manifest_version")
    if not _is_strict_int(manifest_version) or manifest_version != 1:
        raise ManifestError("Field 'manifest_version' must be integer 1")

    corpus_name = _require_non_empty_str(payload, "corpus_name", "manifest")

    raw_docs = payload.get("documents")
    if not isinstance(raw_docs, list) or not raw_docs:
        raise ManifestError("Field 'documents' must be a non-empty list")

    documents: list[ManifestDocument] = []
    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()
    for idx, raw_doc in enumerate(raw_docs):
        doc = _parse_document(raw_doc, idx)
        if doc.doc_id in seen_ids:
            raise ManifestError(f"Duplicate doc_id in manifest: {doc.doc_id!r}")
        if doc.filename in seen_filenames:
            raise ManifestError(f"Duplicate filename in manifest: {doc.filename!r}")
        seen_ids.add(doc.doc_id)
        seen_filenames.add(doc.filename)
        documents.append(doc)

    return CorpusManifest(
        manifest_version=manifest_version,
        corpus_name=corpus_name,
        documents=tuple(documents),
    )


def compute_sha256(path: Path) -> str:
    """Compute the lowercase hexadecimal SHA-256 digest of a file using streaming 64 KiB reads.

    Args:
        path: Path to the file to hash.

    Returns:
        64-character lowercase hexadecimal SHA-256 digest.
    """
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_destination(raw_dir: Path, filename: str, doc_id: str) -> Path:
    """Validate filename and resolve a safe destination path inside raw_dir."""
    safe_name = _validate_filename(filename, f"doc '{doc_id}'")
    resolved_dir = raw_dir.resolve()
    dest = raw_dir / safe_name
    if dest.resolve().parent != resolved_dir:
        raise ManifestError(
            f"doc '{doc_id}': resolved filename {safe_name!r} escapes raw_dir {raw_dir}"
        )
    return dest


def _stream_response_to_part(
    response: _HttpResponse,
    part_path: Path,
    doc: ManifestDocument,
) -> tuple[str, int]:
    """Stream response chunks into part_path, validating %PDF- magic header and hashing."""
    hasher = hashlib.sha256()
    header_buf = bytearray()
    header_verified = False
    size_bytes = 0

    with part_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            if not header_verified:
                header_buf.extend(chunk)
                if len(header_buf) >= len(_PDF_MAGIC):
                    if not bytes(header_buf[: len(_PDF_MAGIC)]) == _PDF_MAGIC:
                        raise FetchError(
                            f"Non-PDF payload received for {doc.doc_id}: missing %PDF- header"
                        )
                    header_verified = True
            handle.write(chunk)
            hasher.update(chunk)
            size_bytes += len(chunk)

    if not header_verified:
        raise FetchError(
            f"Non-PDF or truncated payload received for {doc.doc_id}: missing %PDF- header"
        )

    return hasher.hexdigest(), size_bytes


def _download_with_retries(
    doc: ManifestDocument,
    dest: Path,
    active_session: _HttpSession,
    max_attempts: int,
    backoff_seconds: float,
    timeout_seconds: float,
    sleep_fn: Callable[[float], None],
) -> FetchResult:
    """Download doc to dest atomically with exponential backoff on transient failures."""
    part_path = dest.with_name(f"{dest.name}.part")
    expected_sha = _validate_sha256(doc.sha256, f"doc '{doc.doc_id}'")
    headers = {"User-Agent": _USER_AGENT}

    for attempt in range(1, max_attempts + 1):
        should_retry = False
        try:
            with active_session.get(
                doc.url,
                stream=True,
                timeout=timeout_seconds,
                headers=headers,
            ) as response:
                final_url = getattr(response, "url", None)
                if (
                    isinstance(final_url, str)
                    and final_url
                    and urlparse(final_url).scheme != "https"
                ):
                    raise FetchError(
                        f"Insecure redirect downgrade for {doc.doc_id}: "
                        f"redirected to non-HTTPS URL {final_url!r}"
                    )

                status_code = response.status_code
                if 200 <= status_code < 300:
                    actual_sha, size_bytes = _stream_response_to_part(response, part_path, doc)
                    if actual_sha != expected_sha:
                        part_path.unlink(missing_ok=True)
                        raise ChecksumMismatchError(
                            f"SHA-256 mismatch for {doc.doc_id}: "
                            f"expected {expected_sha}, got {actual_sha}"
                        )
                    os.replace(part_path, dest)
                    return FetchResult(
                        doc_id=doc.doc_id,
                        path=dest,
                        status="downloaded",
                        sha256=actual_sha,
                        size_bytes=size_bytes,
                        attempts=attempt,
                    )

                if status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < max_attempts:
                        should_retry = True
                    else:
                        raise FetchError(
                            f"HTTP {status_code} fetching {doc.doc_id} after {attempt} attempt(s)"
                        )
                else:
                    raise FetchError(f"HTTP {status_code} fetching {doc.doc_id} from {doc.url}")

        except requests.RequestException as exc:
            part_path.unlink(missing_ok=True)
            if attempt < max_attempts:
                should_retry = True
            else:
                raise FetchError(
                    f"Request failed for {doc.doc_id} after {attempt} attempt(s): {exc}"
                ) from exc
        except (ChecksumMismatchError, FetchError):
            part_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            part_path.unlink(missing_ok=True)
            raise FetchError(f"Filesystem error while fetching {doc.doc_id}: {exc}") from exc

        if should_retry:
            sleep_fn(backoff_seconds * (2 ** (attempt - 1)))

    raise FetchError(f"Failed to fetch {doc.doc_id} after {max_attempts} attempt(s)")


def fetch_document(
    doc: ManifestDocument,
    raw_dir: Path = DEFAULT_RAW_DIR,
    force: bool = False,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    timeout_seconds: float = 30.0,
    session: _HttpSession | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """Verify or download a single regulation PDF with SHA-256 integrity checking.

    Args:
        doc: Manifest entry describing the target PDF and expected SHA-256.
        raw_dir: Directory where raw PDFs are stored.
        force: If True, re-download even if a verified local copy already exists.
        max_attempts: Maximum total HTTP download attempts (>= 1).
        backoff_seconds: Initial backoff delay in seconds before retrying.
        timeout_seconds: Per-request timeout in seconds (> 0).
        session: Optional HTTP session (defaults to a fresh requests.Session).
        sleep_fn: Injectable sleep callable for deterministic backoff testing.

    Returns:
        FetchResult describing whether the file was skipped or downloaded.

    Raises:
        ManifestError: If document filename/url/sha256 fails safety validation.
        ChecksumMismatchError: If downloaded bytes do not match doc.sha256.
        FetchError: If network/HTTP/PDF-header validation fails.
        ValueError: If retry or timeout parameters are out of range.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if backoff_seconds < 0:
        raise ValueError("backoff_seconds must be >= 0")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    _validate_https_url(doc.url, f"doc '{doc.doc_id}'")
    expected_sha = _validate_sha256(doc.sha256, f"doc '{doc.doc_id}'")

    raw_path = Path(raw_dir)
    dest = _resolve_destination(raw_path, doc.filename, doc.doc_id)

    if not force and dest.is_file():
        existing_sha = compute_sha256(dest)
        if existing_sha == expected_sha:
            return FetchResult(
                doc_id=doc.doc_id,
                path=dest,
                status="skipped_verified",
                sha256=existing_sha,
                size_bytes=dest.stat().st_size,
                attempts=0,
            )

    raw_path.mkdir(parents=True, exist_ok=True)

    if session is not None:
        return _download_with_retries(
            doc=doc,
            dest=dest,
            active_session=session,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            timeout_seconds=timeout_seconds,
            sleep_fn=sleep_fn,
        )

    with requests.Session() as owned_session:
        return _download_with_retries(
            doc=doc,
            dest=dest,
            active_session=owned_session,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            timeout_seconds=timeout_seconds,
            sleep_fn=sleep_fn,
        )


def fetch_corpus(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    raw_dir: Path = DEFAULT_RAW_DIR,
    doc_ids: Iterable[str] | None = None,
    force: bool = False,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    timeout_seconds: float = 30.0,
    session: _HttpSession | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[FetchResult]:
    """Fetch and verify all (or selected) documents declared in a corpus manifest.

    Args:
        manifest_path: Path to the corpus manifest JSON file.
        raw_dir: Directory where raw PDFs are stored.
        doc_ids: Optional subset of doc_ids to fetch; None fetches all documents.
        force: If True, re-download even when verified local copies exist.
        max_attempts: Maximum HTTP attempts per document.
        backoff_seconds: Initial exponential backoff delay in seconds.
        timeout_seconds: Per-request timeout in seconds.
        session: Optional HTTP session shared across downloads.
        sleep_fn: Injectable sleep callable for retries.

    Returns:
        Ordered list of FetchResult objects corresponding to selected documents.

    Raises:
        ManifestError: If manifest is invalid or any requested doc_id is unknown.
        ChecksumMismatchError: If a downloaded file fails SHA-256 verification.
        FetchError: If downloading a document fails.
    """
    manifest = load_manifest(manifest_path)

    if doc_ids is None:
        selected_docs = list(manifest.documents)
    else:
        if isinstance(doc_ids, str):
            raise ManifestError("doc_ids must be an iterable of document IDs, not a bare string")
        requested = set(doc_ids)
        known_ids = {doc.doc_id for doc in manifest.documents}
        unknown = sorted(requested - known_ids)
        if unknown:
            raise ManifestError(f"Unknown doc_id(s) requested: {', '.join(unknown)}")
        selected_docs = [doc for doc in manifest.documents if doc.doc_id in requested]

    if session is not None:
        return [
            fetch_document(
                doc,
                raw_dir=raw_dir,
                force=force,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                timeout_seconds=timeout_seconds,
                session=session,
                sleep_fn=sleep_fn,
            )
            for doc in selected_docs
        ]

    with requests.Session() as owned_session:
        return [
            fetch_document(
                doc,
                raw_dir=raw_dir,
                force=force,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                timeout_seconds=timeout_seconds,
                session=owned_session,
                sleep_fn=sleep_fn,
            )
            for doc in selected_docs
        ]


__all__ = [
    "ChecksumMismatchError",
    "CorpusManifest",
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_RAW_DIR",
    "FetchError",
    "FetchResult",
    "ManifestDocument",
    "ManifestError",
    "compute_sha256",
    "fetch_corpus",
    "fetch_document",
    "load_manifest",
]
