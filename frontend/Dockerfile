# Build stage
FROM node:24-alpine AS build

WORKDIR /app

COPY ./package*.json ./

RUN npm install # --force

COPY . .

RUN npm run build --omit=dev

# Runtime stage (just hold files)
FROM alpine:latest

WORKDIR /app

COPY --from=build /app/dist/bmrb_extract/browser /data

# Keep it running because Nginx to access /data
# To stop the container, run 'docker compose down frontend' following by 'docker compose up'
CMD ["tail", "-f", "/dev/null"]

