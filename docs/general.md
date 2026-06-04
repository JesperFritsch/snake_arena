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

# dev — api in docker
docker-compose up -d postgres redis api test-runner && cd frontend && npm run dev

# dev — api on host (with reload)
docker-compose up -d postgres redis test-runner && API_RELOAD=1 uv run --env-file .env api
# then in a second terminal:
cd frontend && npm run dev