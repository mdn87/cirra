# Build only from operator-approved inputs, never from an incoming PR.
# The runtime policy pins the resulting local image ID, not this mutable tag.
FROM python:3.11-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir \
    'PyYAML>=6.0.2,<7' 'pytest>=8.3,<10' 'ruff>=0.11,<1' \
    'build>=1.2,<2' 'setuptools>=77' 'PyJWT[crypto]>=2.10,<3'
USER 65534:65534
