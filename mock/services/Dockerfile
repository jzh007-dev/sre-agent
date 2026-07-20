# Single parameterized Dockerfile shared by all 4 mock services.
#
# Build context is `mock/services/` so _shared/ and every service dir sit as
# siblings and can be COPY'd from the same root. Which service to build is
# picked with a build-arg:
#
#   docker build --build-arg SERVICE=checkout -f Dockerfile .
#
# In docker-compose the same effect is achieved via `build.args.SERVICE`.
#
# Why one file instead of four:
# - The four Dockerfiles were 90% identical (base image, deps install, uvicorn CMD)
# - Copying _shared/ into each image needs the context bumped up one level anyway
# - Central place to update Python version, base image, install flags
# Cost: build-arg indirection is slightly less obvious to newcomers.

FROM python:3.11-slim

ARG SERVICE
ENV SERVICE=${SERVICE}

WORKDIR /app

# Deps first (cache-friendly: only reinstalls when requirements.txt changes,
# not on every code edit).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared observability module (imported as `from _shared.observability import ...`)
COPY _shared ./_shared

# Per-service application code.
COPY ${SERVICE}/app.py .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
