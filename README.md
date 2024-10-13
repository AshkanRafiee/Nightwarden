
# AVService

## Overview

AVService is a microservices-based architecture for handling antivirus scanning. It orchestrates multiple services, including an API, load balancers, Nginx for serving static content, Redis for managing API keys and file metadata, workers for processing scanning tasks, and ClamAV as the core scanner (which can be replaced with other antivirus solutions if preferred). This service is containerized using Docker and can be deployed easily with Docker Compose or, with a few adjustments, in Kubernetes (K8s).

The purpose of AVService is to eliminate the need for each development team to build antivirus scanning functionality into their services when handling file uploads. It can also serve as a central antivirus service within a single organization, providing scanning capabilities to multiple applications and services, ensuring consistent and efficient antivirus protection across the organization.

## Project Structure

```bash
.
├── api/                  # API service written in Python (FastAPI or Flask)
│   ├── Dockerfile         # Docker configuration for the API
│   ├── main.py            # Main entry point for the API
│   └── requirements.txt   # Python dependencies for the API
├── services/
│   ├── envoy/             # Envoy proxy for load balanceing API and AV services
│   ├── nginx/             # Nginx service for serving static files
│   ├── redis/             # Used for managing API keys, enforcing rate limits, and storing metadata for file uploads, including file status, hash, and paths.
│   └── clamav/            # ClamAV antivirus configuration files
├── worker/                # Worker service for processing tasks
│   ├── Dockerfile         # Docker configuration for the worker
│   ├── worker.py          # Main worker logic
│   └── requirements.txt   # Python dependencies for the worker
├── tests/                 # Performance and upload test scripts
├── docker-compose.yml     # Docker Compose file to orchestrate services
└── .env                   # Environment variables file (not included in the repo)
```

## Features

- **API**: Handles user requests.
- **Envoy**: Used as a load balancer for both the API and the AV services.
- **Nginx**: Serves static content.
- **Redis**: Used for managing API keys, enforcing rate limits, and storing metadata for file uploads, including file status, hash, and paths.
- **Worker**: Processes tasks such as scanning files.
- **Performance Testing**: Scripts available to test the performance of the upload and scanning processes.

## Requirements

- **Docker** and **Docker Compose**
- **Python 3.x** (if running outside containers)

## Setup

1. Clone the repository:

   ```bash
   git clone <repository-url>
   cd AVService
   ```

2. Create a `.env` file with the necessary environment variables (you can refer to `.env.example`).

3. Build and run the services using Docker Compose:

   ```bash
   docker-compose up --build
   ```

   This will start all the necessary services (API, Nginx, Redis, etc.) in their respective containers.

## Usage

Once the services are running, you can access the following:

- API docs at `http://localhost:8000/docs` (depending on your setup)
- Nginx static page at `https://localhost`

You can test file uploads and antivirus scanning by interacting with the API. Also see the `tests/upload.lua` script for performance test example of service.

When exposing services for other applications or services to use, ensure that only public routes (available via Nginx) are exposed. Admin or sensitive routes should not be exposed externally for security reasons. Use Nginx as a reverse proxy to route external requests only to the necessary public services, and ensure that admin or internal services are restricted from external access. (it is already!)

## Application Flow

1. Explore the available API endpoints and their documentation by visiting the API docs at `http://localhost:8000/docs`.
2. First, generate an API key with the desired rate limit (requests per second or RPS) using the master key provided in the `.env` file.
3. Once you have generated an API key, use it for all subsequent requests to the API.

Make sure to include the API key in the `x-api-key` header in the following format:
`x-api-key: your-api-key`

## Performance Testing

To run the performance tests:

```bash
cd tests
./performance-test.sh
```

This will run a stress test against the upload API.

## Configuration

### Environment Variables

You will need to set up environment variables in a `.env` file for the services. (You should keep this config in a secure way like a secret manager etc.)

Example `.env` file:

```bash
REDIS_PASSWORD="YOUR_REDIS_PASSWORD_HERE"
MASTER_KEY="YOUR_MASTER_KEY_HERE"
```

### Custom Service Configuration

#### Envoy

You can customize the load balancing for both the API and AV services by modifying the following files:

- `services/envoy/envoy-api-lb/envoy.yaml`: Configuration for the API load balancer.
- `services/envoy/envoy-av-lb/envoy.yaml`: Configuration for the AV load balancer.

#### Nginx

To customize Nginx behavior, modify the `services/nginx/nginx.conf` file. This file allows you to configure how static content is served and manage other reverse proxy settings.

#### Redis

Redis configurations can be adjusted in `services/redis/redis.conf`. You can tweak settings like the max memory, eviction policy, and more to suit your needs.

### Worker Service

The worker logic is implemented in `worker/worker.py`. You can adjust the logic for processing tasks here and ensure the proper queue is used.

## Kubernetes Deployment

If you prefer to deploy this application in a Kubernetes environment, you can do so with minimal adjustments. Here are a few key points to consider when deploying in Kubernetes:

1. **Load Balancing**:
   You may not need to use Envoy as a load balancer when deploying in Kubernetes. Kubernetes provides internal load balancers that can handle traffic routing within the cluster or to external services.

2. **Reverse Proxy**:
   Instead of using the Nginx service provided in this repository, you can choose other types of reverse proxies such as Traefik, HAProxy, or Kubernetes' native Ingress controller, depending on your infrastructure and preferences.

3. **Redis and Persistence**:
   Ensure that Redis is properly deployed in Kubernetes with persistent volumes if needed, to avoid data loss. You may also want to use a managed Redis service for added reliability and scalability.

To deploy in Kubernetes, you can create deployment and service YAML files for each service (API, Redis, Worker, etc.) and apply them to your Kubernetes cluster. Make sure to configure proper service discovery, secrets management, and resource limits as per your use case.

## Contributing

Feel free to open issues or submit pull requests for any bugs or improvements.

## License

This project is licensed under the MIT License.
