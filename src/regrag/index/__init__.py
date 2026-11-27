"""Local lexical and dense indexing package (Phase P2)."""

from regrag.index.build import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EMBEDDER_MODEL,
    DEFAULT_FIXED_CHUNK_WORDS,
    DEFAULT_FIXED_OVERLAP_WORDS,
    DEFAULT_INDEX_DIR,
    INDEX_SCHEMA_VERSION,
    DenseEncoder,
    FixedChunkRecord,
    IndexBuildError,
    IndexIntegrityError,
    IndexReport,
    SentenceTransformerEncoder,
    build_fixed_chunks,
    build_index,
    verify_index_integrity,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EMBEDDER_MODEL",
    "DEFAULT_FIXED_CHUNK_WORDS",
    "DEFAULT_FIXED_OVERLAP_WORDS",
    "DEFAULT_INDEX_DIR",
    "INDEX_SCHEMA_VERSION",
    "DenseEncoder",
    "FixedChunkRecord",
    "IndexBuildError",
    "IndexIntegrityError",
    "IndexReport",
    "SentenceTransformerEncoder",
    "build_fixed_chunks",
    "build_index",
    "verify_index_integrity",
]
