import torch
from sentence_transformers import SentenceTransformer


class ImageEmbedder:
    """A CLIP model: images and text land in one vector space. Its own processor does the resize and crop."""

    def __init__(self, name: str, dimension: int):
        self.name = name
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SentenceTransformer(name, device=self.device)
        actual = len(self.model.encode("dimension probe"))  # the CLIP wrapper does not report it
        if actual != dimension:
            raise RuntimeError(f"model_spec.json says {dimension} dimensions, {name} produces {actual}")

    def embed_images(self, images) -> list[list[float]]:
        return self.model.encode(images, normalize_embeddings=True, batch_size=8).tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()
