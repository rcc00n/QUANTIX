FROM node:22-alpine AS dependencies
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

FROM dependencies AS build
COPY frontend ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
RUN addgroup -g 10001 arbiq && adduser -D -u 10001 -G arbiq arbiq
COPY --from=build --chown=arbiq:arbiq /app/.next/standalone ./
COPY --from=build --chown=arbiq:arbiq /app/.next/static ./.next/static
COPY --from=build --chown=arbiq:arbiq /app/public ./public
USER arbiq
EXPOSE 3000
CMD ["node", "server.js"]
