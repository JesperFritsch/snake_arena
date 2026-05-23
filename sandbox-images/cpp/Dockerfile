FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ pkg-config \
    libgrpc++-dev libprotobuf-dev protobuf-compiler-grpc protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd -u 1000 -m agent

COPY sandbox-images/proto/ ./proto/
COPY sandbox-images/cpp/harness/ ./harness/

# Generate proto stubs and pre-compile all non-user translation units.
# Keeps the pre-built .o files in the image so the user build step only
# needs to recompile snake.cpp and re-link.
RUN cd /app/harness && \
    protoc --proto_path=/app/proto \
           --cpp_out=. \
           --grpc_out=. \
           --plugin=protoc-gen-grpc=/usr/bin/grpc_cpp_plugin \
           /app/proto/sim_interface.proto && \
    g++ -std=c++17 -O2 -I. -c sim_interface.pb.cc      -o sim_interface.pb.o      && \
    g++ -std=c++17 -O2 -I. -c sim_interface.grpc.pb.cc -o sim_interface.grpc.pb.o && \
    g++ -std=c++17 -O2 -I. -c main.cpp              -o main.o              && \
    g++ -std=c++17 -O2 -I. -c snake.cpp             -o snake.o             && \
    g++ -o harness sim_interface.pb.o sim_interface.grpc.pb.o main.o snake.o \
        $(pkg-config --libs grpc++ protobuf)

RUN chown -R 1000:1000 /app

USER 1000:1000

EXPOSE 50051
ENTRYPOINT ["/app/harness/harness"]
