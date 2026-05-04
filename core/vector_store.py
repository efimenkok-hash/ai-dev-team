
import faiss
import numpy as np


class VectorStore:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.index = faiss.IndexFlatL2(dim)
        self.items: list[str] = []

    def add(self, texts: list[str], vectors: np.ndarray) -> None:
        if len(texts) == 0:
            return
        self.index.add(vectors.astype("float32"))
        self.items.extend(texts)

    def search(self, vector: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        if self.index.ntotal == 0:
            return []

        distances, ids = self.index.search(
            vector.astype("float32").reshape(1, -1),
            k
        )

        result = []

        for idx, dist in zip(ids[0], distances[0], strict=False):
            if idx >= 0:
                result.append((self.items[idx], float(dist)))

        return result
