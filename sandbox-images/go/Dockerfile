FROM golang:1.23-alpine

ENV GOPATH=/app/.go
ENV GOCACHE=/app/.cache/go-build
ENV PATH="${GOPATH}/bin:${PATH}"

RUN apk add --no-cache protobuf

RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.35.2 && \
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1

WORKDIR /app

RUN adduser -D -u 1000 agent

COPY sandbox-images/proto/ ./proto/
COPY sandbox-images/go/harness/ ./harness/

RUN mkdir -p /app/harness/pb && \
    protoc \
        --go_out=/app/harness/pb --go_opt=paths=source_relative \
        --go-grpc_out=/app/harness/pb --go-grpc_opt=paths=source_relative \
        -I /app/proto \
        /app/proto/sim_interface.proto && \
    cd /app/harness && \
    go mod tidy && \
    go build -o harness .

RUN chown -R 1000:1000 /app

USER 1000:1000

EXPOSE 50051
ENTRYPOINT ["/app/harness/harness"]
