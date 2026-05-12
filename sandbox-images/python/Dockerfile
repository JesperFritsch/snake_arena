FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/harness/stubs

WORKDIR /app

COPY sandbox-images/python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sandbox-images/python/harness/ ./harness/
COPY sandbox-images/proto/ ./proto/

RUN mkdir -p ./harness/stubs && \
    python -m grpc_tools.protoc \
      --proto_path=proto \
      --python_out=./harness/stubs \
      --grpc_python_out=./harness/stubs \
      proto/sim_interface.proto && \
    touch ./harness/stubs/__init__.py

RUN echo ""
RUN useradd -u 1000 -m agent
USER 1000:1000

EXPOSE 50051
ENTRYPOINT ["python", "-m", "harness.main"]