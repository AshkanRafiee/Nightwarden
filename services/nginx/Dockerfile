FROM nginx:latest

# Install OpenSSL
RUN apt-get update && apt-get install -y openssl

# Generate SSL key and certificate
RUN mkdir -p /etc/nginx/ssl && \
    openssl req -x509 -nodes -newkey rsa:2048 -keyout /etc/nginx/ssl/nginx.key -out /etc/nginx/ssl/nginx.crt -days 365 -subj "/CN=localhost"

# Copy custom configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy static files
COPY static /static
