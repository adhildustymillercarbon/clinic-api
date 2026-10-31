# Stage 1: Build the React application
FROM node:20-alpine AS builder

WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm ci

# Copy source code and config
COPY . .

# Accept build arguments for Vite environment variables
ARG VITE_API_KEY=secret-api-key
ARG VITE_BACKEND_URL=http://localhost:8000

ENV VITE_API_KEY=$VITE_API_KEY
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL

# Build the production bundle
RUN npm run build

# Stage 2: Serve the production bundle with Nginx
FROM nginx:alpine

# Copy custom Nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy build artifacts from builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose HTTP port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
