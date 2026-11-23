"""Unit tests for manifest-driven corpus fetching and SHA-256 verification."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import requests

from regrag.corpus.fetch import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_RAW_DIR,
    ChecksumMismatchError,
    CorpusManifest,
    FetchError,
    FetchResult,
    ManifestDocument,
    ManifestError,
    compute_sha256,
    fetch_corpus,
    fetch_document,
    load_manifest,
)

VALID_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
VALID_PDF_SHA256 = hashlib.sha256(VALID_PDF_BYTES).hexdigest()

SECOND_PDF_BYTES = b"%PDF-1.7\n% Second fixture PDF\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
SECOND_PDF_SHA256 = hashlib.sha256(SECOND_PDF_BYTES).hexdigest()


class FakeResponse:
    """Minimal streaming HTTP response stub for deterministic tests."""

    def __init__(
        self,
        status_code: int = 200,
        body: bytes = VALID_PDF_BYTES,
        chunk_size: int = 16,
        url: str = "https://www.fia.com/sites/default/files/fia_2024_technical.pdf",
    ) -> None:
        self.status_code = status_code
        self._body = body
        self._chunk_size = chunk_size
        self.url = url
        self.closed = False

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        step = min(self._chunk_size, chunk_size) if chunk_size > 0 else self._chunk_size
        for offset in range(0, len(self._body), step):
            yield self._body[offset : offset + step]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeSession:
    """Queue-backed HTTP session stub capturing call arguments."""

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(
        self,
        url: str,
        *,
        stream: bool = True,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "url": url,
                "stream": stream,
                "timeout": timeout,
                "headers": dict(headers) if headers is not None else {},
            }
        )
        if not self._responses:
            raise AssertionError(f"Unexpected extra HTTP call to {url}")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _make_doc_dict(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "doc_id": "fia_2024_technical",
        "year": 2024,
        "doc_type": "technical",
        "issue": "Issue 8",
        "published_date": "2024-10-17",
        "title": "FIA 2024 Formula 1 Technical Regulations",
        "filename": "fia_2024_f1_technical_regulations_iss8.pdf",
        "url": "https://www.fia.com/sites/default/files/fia_2024_technical.pdf",
        "sha256": VALID_PDF_SHA256,
        "expected_bytes": len(VALID_PDF_BYTES),
        "expected_pages": 178,
    }
    doc.update(overrides)
    return doc


def _make_manifest_dict(documents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "corpus_name": "fia-f1-regulations",
        "documents": [_make_doc_dict()] if documents is None else documents,
    }


def _write_manifest(tmp_path: Path, payload: Any) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _make_manifest_doc(**overrides: Any) -> ManifestDocument:
    data = _make_doc_dict(**overrides)
    return ManifestDocument(**data)


def test_default_constants() -> None:
    assert Path("corpus/manifest.json") == DEFAULT_MANIFEST_PATH
    assert Path("data/raw") == DEFAULT_RAW_DIR


def test_repo_manifest_loads_all_four_fia_documents(repo_root: Path) -> None:
    manifest = load_manifest(repo_root / "corpus" / "manifest.json")
    assert manifest.manifest_version == 1
    assert manifest.corpus_name == "fia-f1-regulations"
    assert [doc.doc_id for doc in manifest.documents] == [
        "fia_2024_technical",
        "fia_2025_technical",
        "fia_2024_sporting",
        "fia_2025_sporting",
    ]
    assert {doc.year for doc in manifest.documents} == {2024, 2025}
    assert {doc.doc_type for doc in manifest.documents} == {"technical", "sporting"}


def test_load_manifest_valid_and_frozen(tmp_path: Path) -> None:
    docs = [
        _make_doc_dict(),
        _make_doc_dict(
            doc_id="fia_2025_sporting",
            year=2025,
            doc_type="sporting",
            issue="Issue 5",
            published_date="2025-04-30",
            title="FIA 2025 Formula 1 Sporting Regulations",
            filename="fia_2025_f1_sporting_regulations_iss5.pdf",
            url="https://www.fia.com/system/files/documents/fia_2025_sporting.pdf",
            sha256=SECOND_PDF_SHA256.upper(),
            expected_bytes=len(SECOND_PDF_BYTES),
            expected_pages=107,
        ),
    ]
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict(docs))

    manifest = load_manifest(manifest_path)

    assert isinstance(manifest, CorpusManifest)
    assert manifest.manifest_version == 1
    assert manifest.corpus_name == "fia-f1-regulations"
    assert isinstance(manifest.documents, tuple)
    assert len(manifest.documents) == 2
    assert manifest.documents[0].doc_id == "fia_2024_technical"
    assert manifest.documents[1].doc_type == "sporting"
    assert manifest.documents[1].sha256 == SECOND_PDF_SHA256

    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.corpus_name = "mutated"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.documents[0].year = 2099  # type: ignore[misc]


def test_load_manifest_missing_file_raises_manifest_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="not found|read|missing"):
        load_manifest(tmp_path / "does_not_exist.json")


def test_load_manifest_invalid_json_raises_manifest_error(tmp_path: Path) -> None:
    bad_path = tmp_path / "manifest.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON"):
        load_manifest(bad_path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"manifest_version": 0, "corpus_name": "x", "documents": [_make_doc_dict()]},
        {"manifest_version": 2, "corpus_name": "x", "documents": [_make_doc_dict()]},
        {"manifest_version": True, "corpus_name": "x", "documents": [_make_doc_dict()]},
        {"manifest_version": "1", "corpus_name": "x", "documents": [_make_doc_dict()]},
        {"manifest_version": 1, "corpus_name": "", "documents": [_make_doc_dict()]},
        {"manifest_version": 1, "corpus_name": "x", "documents": []},
        {"manifest_version": 1, "corpus_name": "x", "documents": ["not-a-dict"]},
    ],
)
def test_load_manifest_rejects_invalid_top_level(tmp_path: Path, payload: Any) -> None:
    manifest_path = _write_manifest(tmp_path, payload)
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    "missing_field",
    [
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
    ],
)
def test_load_manifest_rejects_missing_document_field(tmp_path: Path, missing_field: str) -> None:
    doc = _make_doc_dict()
    del doc[missing_field]
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict([doc]))

    with pytest.raises(ManifestError, match=missing_field):
        load_manifest(manifest_path)


def test_load_manifest_rejects_duplicate_doc_id(tmp_path: Path) -> None:
    docs = [
        _make_doc_dict(doc_id="dup_id", filename="first.pdf"),
        _make_doc_dict(doc_id="dup_id", filename="second.pdf"),
    ]
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict(docs))

    with pytest.raises(ManifestError, match="duplicate|Duplicate"):
        load_manifest(manifest_path)


def test_load_manifest_rejects_duplicate_filename(tmp_path: Path) -> None:
    docs = [
        _make_doc_dict(doc_id="first_id", filename="same.pdf"),
        _make_doc_dict(doc_id="second_id", filename="same.pdf"),
    ]
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict(docs))

    with pytest.raises(ManifestError, match="duplicate|Duplicate"):
        load_manifest(manifest_path)


@pytest.mark.parametrize("bad_year", ["2024", True, False, 1899, 2101, None])
def test_load_manifest_rejects_invalid_year(tmp_path: Path, bad_year: Any) -> None:
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict([_make_doc_dict(year=bad_year)]))
    with pytest.raises(ManifestError, match="year"):
        load_manifest(manifest_path)


@pytest.mark.parametrize("bad_doc_type", ["financial", "TECHNICAL", "Sporting", "", 123])
def test_load_manifest_rejects_invalid_doc_type(tmp_path: Path, bad_doc_type: Any) -> None:
    manifest_path = _write_manifest(
        tmp_path, _make_manifest_dict([_make_doc_dict(doc_type=bad_doc_type)])
    )
    with pytest.raises(ManifestError, match="doc_type"):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://www.fia.com/regulations.pdf",
        "ftp://www.fia.com/regulations.pdf",
        "file:///etc/passwd",
        "https://",
        "not-a-url",
        "",
    ],
)
def test_load_manifest_rejects_non_https_url(tmp_path: Path, bad_url: str) -> None:
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict([_make_doc_dict(url=bad_url)]))
    with pytest.raises(ManifestError, match="url|https"):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    "bad_sha",
    [
        "abc123",
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "0" * 63 + "!",
        "",
        12345,
    ],
)
def test_load_manifest_rejects_invalid_sha256(tmp_path: Path, bad_sha: Any) -> None:
    manifest_path = _write_manifest(
        tmp_path, _make_manifest_dict([_make_doc_dict(sha256=bad_sha)])
    )
    with pytest.raises(ManifestError, match="sha256"):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    "bad_filename",
    [
        "../escape.pdf",
        "a/b.pdf",
        "a\\b.pdf",
        "regulations.txt",
        ".pdf",
        "C:evil.pdf",
        "nested..file.pdf",
        "",
    ],
)
def test_load_manifest_rejects_unsafe_filename(tmp_path: Path, bad_filename: str) -> None:
    manifest_path = _write_manifest(
        tmp_path, _make_manifest_dict([_make_doc_dict(filename=bad_filename)])
    )
    with pytest.raises(ManifestError, match="filename"):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("expected_bytes", 0),
        ("expected_bytes", -10),
        ("expected_bytes", True),
        ("expected_pages", 0),
        ("expected_pages", -1),
        ("expected_pages", False),
        ("published_date", "2024/10/17"),
        ("published_date", "2024-13-45"),
        ("published_date", ""),
        ("issue", "   "),
        ("title", ""),
        ("doc_id", "   "),
    ],
)
def test_load_manifest_rejects_invalid_metadata_fields(
    tmp_path: Path, field_name: str, bad_value: Any
) -> None:
    manifest_path = _write_manifest(
        tmp_path, _make_manifest_dict([_make_doc_dict(**{field_name: bad_value})])
    )
    with pytest.raises(ManifestError, match=field_name):
        load_manifest(manifest_path)


def test_compute_sha256_streams_multi_chunk_file(tmp_path: Path) -> None:
    payload = b"%PDF-1.7\n" + (b"0123456789abcdef" * 10000)
    assert len(payload) > 131072
    target = tmp_path / "large.pdf"
    target.write_bytes(payload)

    assert compute_sha256(target) == hashlib.sha256(payload).hexdigest()


def test_fetch_document_downloads_and_verifies_pdf(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    session = FakeSession([FakeResponse(status_code=200, body=VALID_PDF_BYTES)])
    sleeps: list[float] = []

    result = fetch_document(
        doc,
        raw_dir=raw_dir,
        session=session,
        sleep_fn=sleeps.append,
    )

    expected_path = raw_dir / doc.filename
    assert isinstance(result, FetchResult)
    assert result.doc_id == doc.doc_id
    assert result.path == expected_path
    assert result.status == "downloaded"
    assert result.sha256 == VALID_PDF_SHA256
    assert result.size_bytes == len(VALID_PDF_BYTES)
    assert result.attempts == 1
    assert expected_path.read_bytes() == VALID_PDF_BYTES
    assert not (raw_dir / f"{doc.filename}.part").exists()
    assert sleeps == []
    assert len(session.calls) == 1
    assert session.calls[0]["stream"] is True
    assert "User-Agent" in session.calls[0]["headers"]
    assert len(session.calls[0]["headers"]["User-Agent"]) > 0


def test_fetch_document_idempotent_skip_zero_http_calls(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / doc.filename
    dest.write_bytes(VALID_PDF_BYTES)
    session = FakeSession([])

    result = fetch_document(doc, raw_dir=raw_dir, session=session)

    assert result.status == "skipped_verified"
    assert result.attempts == 0
    assert result.sha256 == VALID_PDF_SHA256
    assert result.size_bytes == len(VALID_PDF_BYTES)
    assert result.path == dest
    assert session.calls == []


def test_fetch_document_force_redownloads_verified_file(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / doc.filename
    dest.write_bytes(VALID_PDF_BYTES)
    session = FakeSession([FakeResponse(status_code=200, body=VALID_PDF_BYTES)])

    result = fetch_document(doc, raw_dir=raw_dir, force=True, session=session)

    assert result.status == "downloaded"
    assert result.attempts == 1
    assert len(session.calls) == 1
    assert dest.read_bytes() == VALID_PDF_BYTES


def test_fetch_document_checksum_mismatch_mutation_precondition_preserves_existing(
    tmp_path: Path,
) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / doc.filename
    dest.write_bytes(VALID_PDF_BYTES)

    mutated = bytearray(VALID_PDF_BYTES)
    mutated[-2] ^= 0xFF
    mutated_bytes = bytes(mutated)
    mutated_sha = hashlib.sha256(mutated_bytes).hexdigest()
    # Mutation precondition: verify the mutated payload actually changed the digest.
    assert mutated_sha != doc.sha256

    session = FakeSession([FakeResponse(status_code=200, body=mutated_bytes)])

    with pytest.raises(ChecksumMismatchError, match=doc.doc_id):
        fetch_document(doc, raw_dir=raw_dir, force=True, session=session)

    assert not (raw_dir / f"{doc.filename}.part").exists()
    assert list(raw_dir.glob("*.part")) == []
    assert dest.read_bytes() == VALID_PDF_BYTES
    assert compute_sha256(dest) == VALID_PDF_SHA256


def test_fetch_document_recovers_when_existing_local_file_is_corrupt(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / doc.filename
    corrupt_bytes = b"%PDF-1.7\ncorrupted-local-bytes\n"
    dest.write_bytes(corrupt_bytes)
    # Precondition: existing local file fails SHA-256 verification.
    assert compute_sha256(dest) != doc.sha256

    session = FakeSession([FakeResponse(status_code=200, body=VALID_PDF_BYTES)])

    result = fetch_document(doc, raw_dir=raw_dir, force=False, session=session)

    assert result.status == "downloaded"
    assert result.attempts == 1
    assert result.sha256 == VALID_PDF_SHA256
    assert dest.read_bytes() == VALID_PDF_BYTES
    assert compute_sha256(dest) == VALID_PDF_SHA256


def test_fetch_document_retries_transient_503_and_request_exception_with_backoff(
    tmp_path: Path,
) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    session = FakeSession(
        [
            FakeResponse(status_code=503, body=b"Service Unavailable"),
            requests.ConnectionError("connection reset by peer"),
            FakeResponse(status_code=200, body=VALID_PDF_BYTES),
        ]
    )
    sleeps: list[float] = []

    result = fetch_document(
        doc,
        raw_dir=raw_dir,
        max_attempts=3,
        backoff_seconds=0.5,
        session=session,
        sleep_fn=sleeps.append,
    )

    assert result.status == "downloaded"
    assert result.attempts == 3
    assert len(session.calls) == 3
    assert sleeps == [0.5, 1.0]
    assert (raw_dir / doc.filename).read_bytes() == VALID_PDF_BYTES


def test_fetch_document_exhausted_transient_retries_raises_fetch_error(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    session = FakeSession(
        [
            FakeResponse(status_code=503, body=b"unavailable"),
            FakeResponse(status_code=503, body=b"unavailable"),
            FakeResponse(status_code=503, body=b"unavailable"),
        ]
    )
    sleeps: list[float] = []

    with pytest.raises(FetchError, match="503"):
        fetch_document(
            doc,
            raw_dir=raw_dir,
            max_attempts=3,
            backoff_seconds=0.25,
            session=session,
            sleep_fn=sleeps.append,
        )

    assert len(session.calls) == 3
    assert sleeps == [0.25, 0.5]
    assert list(raw_dir.glob("*.part")) == []


def test_fetch_document_fails_fast_on_404(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    session = FakeSession([FakeResponse(status_code=404, body=b"Not Found")])
    sleeps: list[float] = []

    with pytest.raises(FetchError, match="404"):
        fetch_document(
            doc,
            raw_dir=raw_dir,
            max_attempts=3,
            backoff_seconds=1.0,
            session=session,
            sleep_fn=sleeps.append,
        )

    assert len(session.calls) == 1
    assert sleeps == []
    assert not (raw_dir / doc.filename).exists()


def test_fetch_document_rejects_non_pdf_payload_missing_magic_header(tmp_path: Path) -> None:
    html_payload = b"<!DOCTYPE html><html><body>WAF challenge</body></html>"
    html_sha = hashlib.sha256(html_payload).hexdigest()
    # Even if SHA-256 matched the non-PDF bytes, magic-header check must reject it as FetchError.
    doc = _make_manifest_doc(sha256=html_sha, expected_bytes=len(html_payload))
    raw_dir = tmp_path / "raw"
    session = FakeSession([FakeResponse(status_code=200, body=html_payload)])
    sleeps: list[float] = []

    with pytest.raises(FetchError, match="PDF|%PDF-"):
        fetch_document(
            doc,
            raw_dir=raw_dir,
            max_attempts=3,
            session=session,
            sleep_fn=sleeps.append,
        )

    assert len(session.calls) == 1
    assert sleeps == []
    assert not (raw_dir / doc.filename).exists()
    assert list(raw_dir.glob("*.part")) == []


def test_fetch_document_rejects_unsafe_filename_directly(tmp_path: Path) -> None:
    doc = _make_manifest_doc(filename="../escape.pdf")
    session = FakeSession([])

    with pytest.raises(ManifestError, match="filename"):
        fetch_document(doc, raw_dir=tmp_path / "raw", session=session)

    assert session.calls == []


def test_fetch_corpus_downloads_and_second_run_skips(tmp_path: Path) -> None:
    docs = [
        _make_doc_dict(doc_id="doc_a", filename="doc_a.pdf", sha256=VALID_PDF_SHA256),
        _make_doc_dict(
            doc_id="doc_b",
            doc_type="sporting",
            filename="doc_b.pdf",
            sha256=SECOND_PDF_SHA256,
            expected_bytes=len(SECOND_PDF_BYTES),
        ),
    ]
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict(docs))
    raw_dir = tmp_path / "raw"
    session = FakeSession(
        [
            FakeResponse(status_code=200, body=VALID_PDF_BYTES),
            FakeResponse(status_code=200, body=SECOND_PDF_BYTES),
        ]
    )

    first_run = fetch_corpus(manifest_path=manifest_path, raw_dir=raw_dir, session=session)
    assert [r.doc_id for r in first_run] == ["doc_a", "doc_b"]
    assert all(r.status == "downloaded" for r in first_run)
    assert len(session.calls) == 2

    second_run = fetch_corpus(manifest_path=manifest_path, raw_dir=raw_dir, session=session)
    assert [r.doc_id for r in second_run] == ["doc_a", "doc_b"]
    assert all(r.status == "skipped_verified" for r in second_run)
    assert all(r.attempts == 0 for r in second_run)
    assert len(session.calls) == 2


def test_fetch_corpus_filters_by_doc_ids(tmp_path: Path) -> None:
    docs = [
        _make_doc_dict(doc_id="doc_a", filename="doc_a.pdf", sha256=VALID_PDF_SHA256),
        _make_doc_dict(
            doc_id="doc_b",
            doc_type="sporting",
            filename="doc_b.pdf",
            sha256=SECOND_PDF_SHA256,
            expected_bytes=len(SECOND_PDF_BYTES),
        ),
    ]
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict(docs))
    raw_dir = tmp_path / "raw"
    session = FakeSession([FakeResponse(status_code=200, body=SECOND_PDF_BYTES)])

    results = fetch_corpus(
        manifest_path=manifest_path,
        raw_dir=raw_dir,
        doc_ids=["doc_b"],
        session=session,
    )

    assert len(results) == 1
    assert results[0].doc_id == "doc_b"
    assert results[0].status == "downloaded"
    assert not (raw_dir / "doc_a.pdf").exists()
    assert (raw_dir / "doc_b.pdf").read_bytes() == SECOND_PDF_BYTES


def test_fetch_corpus_unknown_doc_id_raises_manifest_error(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict([_make_doc_dict()]))
    session = FakeSession([])

    with pytest.raises(ManifestError, match="unknown_doc"):
        fetch_corpus(
            manifest_path=manifest_path,
            raw_dir=tmp_path / "raw",
            doc_ids=["unknown_doc"],
            session=session,
        )

    assert session.calls == []


def test_fetch_corpus_bare_string_doc_ids_raises_manifest_error(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, _make_manifest_dict([_make_doc_dict()]))
    session = FakeSession([])

    with pytest.raises(ManifestError, match="iterable"):
        fetch_corpus(
            manifest_path=manifest_path,
            raw_dir=tmp_path / "raw",
            doc_ids="fia_2024_technical",
            session=session,
        )

    assert session.calls == []


def test_fetch_document_exhausted_request_exception_raises_fetch_error(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    session = FakeSession(
        [
            requests.Timeout("read timed out"),
            requests.Timeout("read timed out"),
        ]
    )
    sleeps: list[float] = []

    with pytest.raises(FetchError, match="Request failed"):
        fetch_document(
            doc,
            raw_dir=raw_dir,
            max_attempts=2,
            backoff_seconds=0.5,
            session=session,
            sleep_fn=sleeps.append,
        )

    assert len(session.calls) == 2
    assert sleeps == [0.5]
    assert list(raw_dir.glob("*.part")) == []


def test_fetch_document_rejects_truncated_short_payload(tmp_path: Path) -> None:
    short_payload = b"%PD"
    doc = _make_manifest_doc(
        sha256=hashlib.sha256(short_payload).hexdigest(),
        expected_bytes=len(short_payload),
    )
    session = FakeSession([FakeResponse(status_code=200, body=short_payload)])

    with pytest.raises(FetchError, match="PDF|%PDF-"):
        fetch_document(doc, raw_dir=tmp_path / "raw", session=session)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"backoff_seconds": -0.1}, "backoff_seconds"),
        ({"timeout_seconds": 0.0}, "timeout_seconds"),
    ],
)
def test_fetch_document_rejects_invalid_retry_and_timeout_params(
    tmp_path: Path, kwargs: dict[str, Any], match: str
) -> None:
    doc = _make_manifest_doc()
    with pytest.raises(ValueError, match=match):
        fetch_document(doc, raw_dir=tmp_path / "raw", session=FakeSession([]), **kwargs)


def test_fetch_document_closes_response_before_backoff_sleep(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    first_resp = FakeResponse(status_code=503, body=b"Service Unavailable")
    second_resp = FakeResponse(status_code=200, body=VALID_PDF_BYTES)
    session = FakeSession([first_resp, second_resp])
    closed_states_during_sleep: list[bool] = []
    sleeps: list[float] = []

    def recording_sleep(seconds: float) -> None:
        closed_states_during_sleep.append(first_resp.closed)
        sleeps.append(seconds)

    result = fetch_document(
        doc,
        raw_dir=raw_dir,
        max_attempts=2,
        backoff_seconds=0.5,
        session=session,
        sleep_fn=recording_sleep,
    )

    assert result.status == "downloaded"
    assert result.attempts == 2
    assert sleeps == [0.5]
    assert closed_states_during_sleep == [True]
    assert second_resp.closed is True


def test_fetch_document_rejects_https_to_http_redirect_downgrade(tmp_path: Path) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    downgraded = FakeResponse(
        status_code=200,
        body=VALID_PDF_BYTES,
        url="http://www.fia.com/downgraded.pdf",
    )
    session = FakeSession([downgraded])
    sleeps: list[float] = []

    with pytest.raises(FetchError, match="HTTPS|downgrade|redirect"):
        fetch_document(
            doc,
            raw_dir=raw_dir,
            max_attempts=3,
            session=session,
            sleep_fn=sleeps.append,
        )

    assert len(session.calls) == 1
    assert sleeps == []
    assert downgraded.closed is True
    assert not (raw_dir / doc.filename).exists()
    assert list(raw_dir.glob("*.part")) == []


def test_fetch_document_oserror_cleans_up_part_file_and_preserves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / doc.filename
    dest.write_bytes(VALID_PDF_BYTES)
    session = FakeSession([FakeResponse(status_code=200, body=VALID_PDF_BYTES)])

    def failing_replace(src: Path | str, dst: Path | str) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(FetchError, match="Filesystem error"):
        fetch_document(doc, raw_dir=raw_dir, force=True, session=session)

    assert not (raw_dir / f"{doc.filename}.part").exists()
    assert list(raw_dir.glob("*.part")) == []
    assert dest.read_bytes() == VALID_PDF_BYTES


def test_fetch_document_and_corpus_with_session_none_opens_and_closes_owned_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_sessions: list[FakeSession] = []

    def fake_session_factory() -> FakeSession:
        sess = FakeSession([FakeResponse(status_code=200, body=VALID_PDF_BYTES)])
        created_sessions.append(sess)
        return sess

    monkeypatch.setattr(requests, "Session", fake_session_factory)

    doc = _make_manifest_doc()
    raw_dir = tmp_path / "raw_doc"
    doc_result = fetch_document(doc, raw_dir=raw_dir, session=None)
    assert doc_result.status == "downloaded"
    assert len(created_sessions) == 1
    assert created_sessions[0].closed is True

    manifest_path = _write_manifest(tmp_path, _make_manifest_dict([_make_doc_dict()]))
    corpus_dir = tmp_path / "raw_corpus"
    corpus_results = fetch_corpus(manifest_path=manifest_path, raw_dir=corpus_dir, session=None)
    assert len(corpus_results) == 1
    assert corpus_results[0].status == "downloaded"
    assert len(created_sessions) == 2
    assert created_sessions[1].closed is True
