FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

WORKDIR /app

# Create non-root user matching host UID 1000 (ted)
RUN groupadd -g 1000 ted && useradd -u 1000 -g 1000 -s /sbin/nologin -M ted

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8485

USER 1000

CMD ["python", "-m", "src.server"]
