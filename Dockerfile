FROM python:3.12-slim AS runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
COPY . .
RUN mkdir -p /app/models
ADD --checksum=sha256:57d1997790d1744fba5b40a7317df71ea5e2acee28c47e78f0cce39c0703f8cf https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF/resolve/main/Qwen3.5-0.8B-Q4_0.gguf /app/models/qwen3.5-0.8b-q4_0.gguf
ENV LOCAL_MODEL_PATH=/app/models/qwen3.5-0.8b-q4_0.gguf \
    LOCAL_MODEL_NAME=qwen3.5-0.8b-q4_0 \
    LOCAL_MODEL_CONTEXT=1536 \
    LOCAL_MODEL_THREADS=2
EXPOSE 8080
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
