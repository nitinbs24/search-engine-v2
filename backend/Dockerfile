# Build context is the repo root: the proto lives in shared/proto.
FROM golang:1.23-alpine AS build
RUN apk add --no-cache protobuf protobuf-dev && \
    go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2 && \
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1
WORKDIR /src
COPY backend/go.mod backend/go.sum ./
RUN go mod download
COPY backend/ ./
COPY shared/proto/search.proto /proto/
RUN mkdir -p proto && protoc -I /proto --go_out=proto --go_opt=paths=source_relative \
    --go-grpc_out=proto --go-grpc_opt=paths=source_relative search.proto
RUN CGO_ENABLED=0 go build -o /backend .

# `make test` builds this stage.
FROM build AS test
RUN go vet ./... && go test ./...

FROM alpine:3.20
COPY --from=build /backend /backend
EXPOSE 8080
CMD ["/backend"]
