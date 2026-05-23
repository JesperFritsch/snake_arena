FROM rust:slim

ENV CARGO_HOME=/app/.cargo

WORKDIR /app

RUN apt-get update && apt-get install -y protobuf-compiler && rm -rf /var/lib/apt/lists/*

RUN useradd -u 1000 -m agent

COPY sandbox-images/proto/ ./proto/
COPY sandbox-images/rust/harness/ ./harness/

# Pre-compile with the stub snake.rs to cache all dependency artifacts.
# The user build step only needs to recompile snake.rs and re-link.
RUN cd harness && cargo build --release

# Hand ownership to the agent user so the user build step (which runs as
# user 1000) can overwrite src/ and write new artifacts into target/.
RUN chown -R 1000:1000 /app

USER 1000:1000

EXPOSE 50051
ENTRYPOINT ["/app/harness/target/release/harness"]
