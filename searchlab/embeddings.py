"""Turn text into vectors with a real embedding model.

The lab could already generate *synthetic* vectors — clustered random
points that give HNSW something realistically shaped to chew on. That
answers "how does ANN behave under load" but not "does this actually
find the right documents", because random vectors have no meaning to be
right about. This module closes that gap: index real text, search it by
meaning, and measure recall against ground truth that means something.

Backed by fastembed (ONNX) rather than sentence-transformers, which
pulls in torch. Same models, ~158 MB instead of ~2.5 GB — the difference
between a lab that runs on a laptop and one that doesn't. The dependency
is optional either way: `pip install 'searchlab[embed]'`.
"""

from __future__ import annotations

import threading

# name -> (model id, dimensions). Dimensions matter because the Solr
# DenseVectorField is declared with a fixed size — a mismatch is rejected
# at index time, so the UI needs to know before it creates the field.
MODELS: dict[str, tuple[str, int]] = {
    "minilm": ("sentence-transformers/all-MiniLM-L6-v2", 384),
    "bge-small": ("BAAI/bge-small-en-v1.5", 384),
    "bge-base": ("BAAI/bge-base-en-v1.5", 768),
    "nomic": ("nomic-ai/nomic-embed-text-v1.5", 768),
}
DEFAULT_MODEL = "minilm"

INSTALL_HINT = ("Embedding support isn't installed. Run:  "
                "pip install 'searchlab[embed]'")


class Embedder:
    """A loaded model. Loading downloads weights on first use (~90 MB for
    the small ones), so callers should treat construction as slow."""

    def __init__(self, name: str = DEFAULT_MODEL):
        if name not in MODELS:
            raise ValueError(
                f"Unknown model {name!r}. Available: {', '.join(MODELS)}")
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise RuntimeError(INSTALL_HINT) from None
        self.name = name
        self.model_id, self.dims = MODELS[name]
        self._model = TextEmbedding(self.model_id)
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Vectors for each text, as plain lists ready for JSON.

        fastembed returns L2-normalized vectors, which is what cosine
        similarity wants — so no extra normalization step here.
        """
        if not texts:
            return []
        # the underlying session isn't documented as thread-safe, and the
        # dashboard can call this from several request threads at once
        with self._lock:
            return [v.tolist() for v in self._model.embed(texts)]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


_loaded: dict[str, Embedder] = {}
_load_lock = threading.Lock()


def get(name: str = DEFAULT_MODEL) -> Embedder:
    """Load once, reuse after — the model is expensive to construct."""
    with _load_lock:
        if name not in _loaded:
            _loaded[name] = Embedder(name)
        return _loaded[name]


def loaded_names() -> list[str]:
    return sorted(_loaded)


def available() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def vector_profile(dims: int, similarity: str = "cosine",
                   field: str = "vec") -> dict:
    """A one-field profile describing the vector field, so schema.py can
    create the DenseVectorField without a YAML file on disk."""
    return {"fields": {field: {"type": "vector", "dims": dims,
                               "similarity": similarity}}}


def knn_query(field: str, vector: list[float], top_k: int = 10) -> str:
    """Solr's kNN syntax: {!knn f=vec topK=10}[0.1,0.2,...]"""
    body = ",".join(f"{v:.6f}" for v in vector)
    return f"{{!knn f={field} topK={top_k}}}[{body}]"
