FROM node:20-slim

ENV NODE_ENV=production

WORKDIR /app

COPY sandbox-images/proto/ ./proto/

# Install npm deps before copying source so this layer is cached independently
# of source code changes.
COPY sandbox-images/js/harness/package.json ./harness/package.json
RUN cd /app/harness && npm install --omit=dev && npm cache clean --force

COPY sandbox-images/js/harness/ ./harness/

RUN chown -R node:node /app

USER node

EXPOSE 50051
ENTRYPOINT ["node", "/app/harness/main.js"]
