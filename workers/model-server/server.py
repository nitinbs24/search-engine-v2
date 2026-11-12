"""Model server: the only place models are loaded. Models and dimensions come from model_spec.json."""
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, StringConstraints

SPEC = json.loads((Path(__file__).parent / "model_spec.json").read_text())
text_embedder = None
reranker = None
image_embedder = None

# Blank input is a 422, never a zero vector.
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@asynccontextmanager
async def lifespan(app):
    global text_embedder, image_embedder, reranker
    from image_embedder import ImageEmbedder  # imported here so tests need no torch
    from reranker import Reranker
    from text_embedder import TextEmbedder

    text_embedder = TextEmbedder(**SPEC["text"])
    image_embedder = ImageEmbedder(**SPEC["image"])
    reranker = Reranker(**SPEC["reranker"])
    yield


app = FastAPI(title="Model server", lifespan=lifespan)


class TextsIn(BaseModel):
    texts: list[NonBlank] = Field(min_length=1, max_length=256)


class QueryIn(BaseModel):
    query: NonBlank


class RerankIn(BaseModel):
    query: NonBlank
    passages: list[str] = Field(min_length=1, max_length=256)


@app.post("/embed/text")
def embed_text(body: TextsIn):
    return {"embeddings": text_embedder.embed(body.texts)}


@app.post("/embed/query")
def embed_query(body: QueryIn):
    return {"text_vector": text_embedder.embed([body.query])[0],
            "image_vector": image_embedder.embed_texts([body.query])[0]}


@app.post("/embed/image")
async def embed_image(request: Request):
    """The request body is the image file itself."""
    try:
        image = Image.open(io.BytesIO(await request.body()))
        image.load()
    except (UnidentifiedImageError, OSError):  # an empty body is also "cannot identify"
        raise HTTPException(422, "not a decodable image")
    return {"embedding": image_embedder.embed_images([image.convert("RGB")])[0]}


@app.post("/rerank")
def rerank(body: RerankIn):
    return {"scores": reranker.score(body.query, body.passages)}


@app.get("/health")
def health():
    loaded = {"text": text_embedder, "image": image_embedder, "reranker": reranker}
    if None in loaded.values():
        raise HTTPException(503, "models not loaded")
    return {"status": "ok", "device": text_embedder.device, "models": {k: m.name for k, m in loaded.items()}}
