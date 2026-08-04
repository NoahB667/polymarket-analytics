FROM python:3.11-slim AS builder

WORKDIR /build

# Install full compilation suite needed for pybind11 / C++
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    libboost-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy source code to compile the binary
COPY . .

# Run the native compilation steps
RUN mkdir -p cpp/build && cd cpp/build \
    && cmake -DCMAKE_BUILD_TYPE=Release .. \
    && make polymarket_core -j$(nproc)

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your Python application source files
COPY . .

# Install the private signal-core submodule (vendor/signal-core) as a package.
# Requires the submodule to be checked out in the build context:
#   git submodule update --init
RUN test -f vendor/signal-core/pyproject.toml || (echo "vendor/signal-core submodule not initialized (run: git submodule update --init)" >&2; exit 1) && pip install --no-cache-dir -e vendor/signal-core

# Move the compiled binary into Python's global site-packages folder
COPY --from=builder /build/cpp/build/polymarket_core*.so /usr/local/lib/python3.11/site-packages/

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]