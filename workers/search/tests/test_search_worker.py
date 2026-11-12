from concurrent import futures
from datetime import datetime, timedelta, timezone

import grpc
import httpx
import pytest
import search_pb2
import search_pb2_grpc

import filters
from main import SearchWorker
from model_client import ModelClient

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
MB = 1024 ** 2


def resolved(*strings):
    return filters.resolve(list(strings), now=NOW, tz="Asia/Kolkata")


def test_extension_filter():
    assert list(resolved("extension:pdf").extensions) == ["pdf"]


def test_image_type_becomes_a_mime_prefix():
    assert list(resolved("type:image").mime_prefixes) == ["image/"]


def test_spreadsheet_type_covers_extensions_and_mime():
    f = resolved("type:spreadsheet")
    assert set(f.extensions) == {"xls", "xlsx", "csv"} and list(f.mime_prefixes) == ["application/vnd.ms-excel"]


def test_size_filters():
    f = resolved("size:size_gt:5MB", "size:size_lt:1.5GB")
    assert (f.min_size_bytes, f.max_size_bytes) == (5 * MB, int(1.5 * 1024 ** 3))


def test_last_week_is_the_seven_days_before_now():
    f = resolved("date:last_week")
    assert (f.uploaded_from, f.uploaded_to) == ((NOW - timedelta(days=7)).isoformat(), NOW.isoformat())


def test_exact_date_is_a_calendar_day_in_the_configured_timezone():
    f = resolved("exact_date:march_4_2026")
    assert (f.uploaded_from, f.uploaded_to) == ("2026-03-04T00:00:00+05:30", "2026-03-05T00:00:00+05:30")


def test_unknown_and_month_filters_restrict_nothing():
    assert resolved("month:java", "nonsense") == search_pb2.Filters()


class FakeModel:
    def __init__(self):
        self.seen = []

    def embed_query(self, text):
        self.seen.append(text)
        return {"text_vector": [0.25, 0.75], "image_vector": [0.5, 0.5, 0.0]}

    def rerank(self, query, passages):
        self.seen.append((query, passages))
        return [float(len(p)) for p in passages]


@pytest.fixture
def stub():
    model = FakeModel()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    search_pb2_grpc.add_SearchWorkerServicer_to_server(SearchWorker(model), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = search_pb2_grpc.SearchWorkerStub(channel)
        stub.model = model
        yield stub
    server.stop(0)


def test_parse_query_round_trip_over_grpc(stub):
    r = stub.ParseQuery(search_pb2.ParseQueryRequest(query="find pdf reports about kubernetes larger than 5MB"))
    assert "kubernetes" in r.keywords
    assert list(r.filters.extensions) == ["pdf"] and r.filters.min_size_bytes == 5 * MB
    assert list(r.text_vector) == [0.25, 0.75]
    assert list(r.image_vector) == [0.5, 0.5, 0.0]
    assert "kubernetes" in stub.model.seen[0]
    assert r.clean_query == stub.model.seen[0] and "5MB" not in r.clean_query


def test_blank_query_is_rejected(stub):
    with pytest.raises(grpc.RpcError) as e:
        stub.ParseQuery(search_pb2.ParseQueryRequest(query="   "))
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_rerank_round_trip_over_grpc(stub):
    r = stub.Rerank(search_pb2.RerankRequest(query="kubernetes", passages=["a", "bbb", ""]))
    assert list(r.scores) == [1.0, 3.0, 0.0]
    assert stub.model.seen[0] == ("kubernetes", ["a", "bbb", ""])


@pytest.mark.parametrize("req", [search_pb2.RerankRequest(query="  ", passages=["a"]), search_pb2.RerankRequest(query="q")])
def test_rerank_rejects_blank_query_and_no_passages(stub, req):
    with pytest.raises(grpc.RpcError) as e:
        stub.Rerank(req)
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_model_client_returns_both_query_vectors():
    body = {"text_vector": [1.0], "image_vector": [2.0, 3.0]}
    client = ModelClient("http://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert client.embed_query("cats") == body
