"""Model server: the only place models are loaded. Models and dimensions come from model_spec.json."""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StringConstraints

SPEC = json.loads((Path(__file__).parent / "model_spec.json").read_text())
text_embedder = None

# Blank input is a 422, never a zero vector.
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@asynccontextmanager
async def lifespan(app):
    global text_embedder
    from text_embedder import TextEmbedder  # imported here so tests need no torch

    text_embedder = TextEmbedder(**SPEC["text"])
    yield


app = FastAPI(title="Model server", lifespan=lifespan)


class TextsIn(BaseModel):
    texts: list[NonBlank] = Field(min_length=1, max_length=256)


class QueryIn(BaseModel):
    query: NonBlank


@app.post("/embed/text")
def embed_text(body: TextsIn):
    return {"embeddings": text_embedder.embed(body.texts)}


@app.post("/embed/query")
def embed_query(body: QueryIn):
    return {"text_vector": text_embedder.embed([body.query])[0]}


@app.get("/health")
def health():
    if text_embedder is None:
        raise HTTPException(503, "models not loaded")
    return {"status": "ok", "device": text_embedder.device, "models": {"text": text_embedder.name}}
