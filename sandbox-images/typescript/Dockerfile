FROM node:20-slim

WORKDIR /app

COPY sandbox-images/proto/ ./proto/

# Install all deps (including devDeps for tsc) before copying source
COPY sandbox-images/typescript/harness/package.json ./harness/package.json
RUN cd /app/harness && npm install && npm cache clean --force

COPY sandbox-images/typescript/harness/ ./harness/

# Pre-compile harness (including stub usercode/snake.ts — user build replaces and recompiles)
RUN cd /app/harness && npx tsc

RUN chown -R node:node /app

USER node

EXPOSE 50051
ENTRYPOINT ["node", "/app/harness/dist/main.js"]
