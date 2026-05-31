docker build --no-cache -f sim-image/Dockerfile -t snake-sim .


docker build --no-cache -f own-snake-images/Dockerfile -t best-snake .

# let containers reach out to host
sudo ufw allow from 172.16.0.0/12 to any port 6000 proto tcp comment "snake_arena runner callback"


uv pip install --force-reinstall 'snake_sim @ git+https://github.com/JesperFritsch/snake_sim.git@master'

# from root
docker compose up --scale runner=4 --scale builder=2