import json
import pathlib

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
        return 200, {"docs": {"mappings": self.store[path]["mappings"]}}


def test_mapping_dimension_comes_from_model_spec():
    field = init.index_body(SPEC)["mappings"]["properties"]["text_embedding"]
    assert field["dimension"] == SPEC["text"]["dimension"]
    assert field["method"]["engine"] == "lucene"


def test_index_is_single_shard_without_replicas():
    settings = init.index_body(SPEC)["settings"]["index"]
    assert (settings["number_of_shards"], settings["number_of_replicas"]) == (1, 0)


def test_init_is_idempotent():
    os_ = FakeOpenSearch()
    init.ensure(os_, "docs", SPEC)
    init.ensure(os_, "docs", SPEC)
    assert os_.calls.count(("PUT", "/docs")) == 1


def test_init_refuses_an_index_built_for_another_dimension():
    os_ = FakeOpenSearch()
    init.ensure(os_, "docs", SPEC)
    other = {"text": {**SPEC["text"], "dimension": SPEC["text"]["dimension"] + 1}}
    try:
        init.ensure(os_, "docs", other)
    except SystemExit as e:
        assert "dimension" in str(e)
    else:
        raise AssertionError("expected a refusal")
