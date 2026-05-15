FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git g++ \
    && pip install --no-cache-dir "snake_sim @ git+https://github.com/JesperFritsch/snake_sim.git@master" \
    && apt-get purge -y git g++ \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# don't need a non-root user as urgently here since the sim is your trusted code,
# but still good hygiene
RUN useradd -u 1000 -m sim
USER 1000:1000

ENTRYPOINT ["python", "-m", "snake_sim.server.grpc_snake_server", "-t", "0.0.0.0:50051", "-c", "snake_config", "--log-level", "DEBUG", "--log-dir", "/tmp"]