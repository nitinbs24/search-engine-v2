"""Search worker: parses a query and embeds it. The backend builds and runs the search."""
import os
from concurrent import futures
from datetime import datetime, timezone

import grpc
import search_pb2
import search_pb2_grpc

import filters
from nlp_parser import NLPQueryParser


class SearchWorker(search_pb2_grpc.SearchWorkerServicer):
    def __init__(self, model):
        self.model = model
        self.parser = NLPQueryParser()
        self.tz = os.getenv("SEARCH_TIMEZONE", "Asia/Kolkata")

    def ParseQuery(self, request, context):
        query = request.query.strip()
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is empty")
        intent, keywords, filter_strings = self.parser.parse(query)
        return search_pb2.ParseQueryResponse(
            keywords=keywords,
            filters=filters.resolve(filter_strings, datetime.now(timezone.utc), self.tz),
            text_vector=self.model.embed_query(intent or query),
        )


if __name__ == "__main__":
    from model_client import ModelClient

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    search_pb2_grpc.add_SearchWorkerServicer_to_server(SearchWorker(ModelClient(os.environ["MODEL_SERVER_URL"])), server)
    server.add_insecure_port("0.0.0.0:50051")
    server.start()
    print("search worker listening on :50051", flush=True)
    server.wait_for_termination()
