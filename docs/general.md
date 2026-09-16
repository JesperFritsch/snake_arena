docker build --no-cache -f sim-image/Dockerfile -t snake-sim .


docker build --no-cache -f own-snake-images/Dockerfile -t best-snake .

# let containers reach out to host
sudo ufw allow from 172.16.0.0/12 to any port 6000 proto tcp comment "gridsnake runner callback"


uv pip install --force-reinstall 'snake_sim @ git+https://github.com/JesperFritsch/snake_sim.git@master'

# from root
docker compose up --scale runner=4 --scale builder=2

# build frontend
cd frontend && VITE_API_BASE_URL=https://gridsnake.com/api npm run build && cd ..


# prod
docker-compose up -d


# dev — docker: postgres, redis, file-server, test-runner; host: API (auto-reload) + vite

# env from .env.dev (+ optional gitignored .env.dev.local); bundles go to ./sim-artifacts via file-server on :8081

# requires gVisor (runsc) installed and registered with docker

./scripts/dev_start.sh   

# frontend: http://localhost:5173, API: http://127.0.0.1:8000

# ctrl-c stops API + frontend; docker services keep running

docker compose --env-file .env.dev -f docker-compose.yml -f docker-compose.dev.yml stop
