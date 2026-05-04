"""
core/code_retriever.py

Semantic code retrieval over a project root using sentence-transformers
and FAISS.

CONTRACTS:
1. build_index(root) lists project files via list_project_files(root) and
   reads each file by joining (root / rel) — never relies on cwd. This
   makes the retriever safe to call from any working directory.
2. Empty files (after strip) are skipped and not embedded.
3. File contents are truncated to MAX_CHARS_PER_FILE before embedding to
   keep memory bounded.
4. search(query, k) returns at most k items; an empty index returns [].
"""

import numpy as np
from sentence_transformers import SentenceTransformer

from core.repo_reader import list_project_files
from core.vector_store import VectorStore

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
MAX_CHARS_PER_FILE = 8000


class CodeRetriever:
    def __init__(self) -> None:
        self.model = SentenceTransformer(MODEL_NAME)
        self.store = VectorStore(dim=EMBEDDING_DIM)
        self.paths: list[str] = []

    def build_index(self, root: str = ".") -> None:
        from pathlib import Path

        base = Path(root)
        files = list_project_files(root)
        texts: list[str] = []
        kept_paths: list[str] = []

        for rel in files:
            full = base / rel
            try:
                content = full.read_text(encoding="utf-8")
            except OSError:
                continue
            except UnicodeDecodeError:
                continue

            if not content.strip():
                continue

            texts.append(content[:MAX_CHARS_PER_FILE])
            kept_paths.append(rel)

        if not texts:
            return

        vectors = self.model.encode(texts, convert_to_numpy=True)
        self.store.add(kept_paths, vectors)
        self.paths = kept_paths

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        vector = self.model.encode([query], convert_to_numpy=True)[0]
        return self.store.search(np.array(vector), k=k)
