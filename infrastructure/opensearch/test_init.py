import json
import pathlib

import pytest

import init

SPEC = json.loads((pathlib.Path(__file__).parents[2] / "workers/model-server/model_spec.json").read_text())


class FakeOpenSearch:
    def __init__(self):
        self.store = {}
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "PUT":
            self.store[path] = body
            return 200, {}
        if path not in self.store:
            return 404, {}
        return 200, {path.lstrip("/"): {"mappings": self.store[path]["mappings"]}}


def test_mapping_dimension_comes_from_model_spec():
    field = init.index_body(SPEC)["mappings"]["properties"]["text_embedding"]
    assert field["dimension"] == SPEC["text"]["dimension"]
    assert field["method"]["engine"] == "lucene"


def test_image_embedding_field_comes_from_model_spec_next_to_text_embedding():
    props = init.index_body(SPEC)["mappings"]["properties"]
    assert props["image_embedding"]["dimension"] == SPEC["image"]["dimension"]
    assert props["image_embedding"]["method"] == {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}
    assert props["text_embedding"]["dimension"] == SPEC["text"]["dimension"]


def test_index_is_single_shard_without_replicas():
    settings = init.index_body(SPEC)["settings"]["index"]
    assert (settings["number_of_shards"], settings["number_of_replicas"]) == (1, 0)


def test_init_is_idempotent():
    os_ = FakeOpenSearch()
    init.ensure(os_, "docs", SPEC)
    init.ensure(os_, "docs", SPEC)
    assert os_.calls.count(("PUT", "/docs")) == 1


@pytest.mark.parametrize("kind", ["text", "image"])
def test_init_refuses_an_index_built_for_another_dimension(kind):
    os_ = FakeOpenSearch()
    init.ensure(os_, "docs", SPEC)
    other = {**SPEC, kind: {**SPEC[kind], "dimension": SPEC[kind]["dimension"] + 1}}
    with pytest.raises(SystemExit, match=f"{kind}_embedding.*dimension"):
        init.ensure(os_, "docs", other)


def test_init_refuses_an_index_that_has_no_image_field_yet():
    os_ = FakeOpenSearch()
    init.ensure(os_, "docs", SPEC)
    del os_.store["/docs"]["mappings"]["properties"]["image_embedding"]
    with pytest.raises(SystemExit, match="image_embedding.*delete the index"):
        init.ensure(os_, "docs", SPEC)
