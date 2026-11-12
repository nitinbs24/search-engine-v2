import torch
from sentence_transformers import SentenceTransformer


class TextEmbedder:
    def __init__(self, name: str, dimension: int):
        self.name = name
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SentenceTransformer(name, device=self.device)
        actual = self.model.get_sentence_embedding_dimension()
        if actual != dimension:
            raise RuntimeError(f"model_spec.json says {dimension} dimensions, {name} produces {actual}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Normalised, so cosine similarity and dot product agree.
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()
