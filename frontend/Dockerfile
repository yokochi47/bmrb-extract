# Build stage
FROM node:24-alpine AS build

WORKDIR /app

COPY ./package*.json ./
RUN npm install

COPY . .
RUN npm run build --omit=dev

# Runtime stage (just hold files)
FROM alpine:latest

WORKDIR /app
COPY --from=build /app/dist /app/dist
