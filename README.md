# Distributed Online Auction System

A distributed online auction platform built using Python, gRPC, Docker, and Ollama (Llama 3.2 3B).

## Prerequisites

- Docker Desktop
- Ollama

## Installation

### 1. Clone the Repository

```bash
git clone <YOUR_REPOSITORY_URL>
cd distributedOnlineAuctionSystem
```

### 2. Configure Environment Variables

Copy the contents of `.env.example` into a new `.env` file in the project root directory.

### 3. Install Docker

Download and install Docker Desktop:

https://www.docker.com/products/docker-desktop/

Make sure Docker Desktop is running.

### 4. Install Ollama

Download and install Ollama:

https://ollama.com/download

Pull the required model:

```bash
ollama pull llama3.2:3b
```

Verify the installation:

```bash
ollama list
```

Keep Ollama running while using the application.

## Running the Project

Start all services using Docker Compose:

```bash
docker compose up --build
```

Once the containers are running, access the application at:

**http://localhost:8000**

## Stopping the Project

```bash
docker compose down
```

**Note:** The LLM service communicates with Ollama running on the host machine through `http://host.docker.internal:11434`.
