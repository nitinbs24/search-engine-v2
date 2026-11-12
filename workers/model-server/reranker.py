import torch
from sentence_transformers import CrossEncoder


class Reranker:
    def __init__(self, name: str):
        self.name = name
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = CrossEncoder(name, device=device)

    def score(self, query: str, passages: list[str]) -> list[float]:
        # Identity: raw logits, not the sigmoid predict() applies by default; the backend's cut-off is on these.
        return self.model.predict([(query, p) for p in passages], batch_size=32, activation_fct=torch.nn.Identity()).tolist()
