FROM python:3.12-slim AS runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
COPY . .
RUN mkdir -p /app/models
ADD --checksum=sha256:8070f21b9763ef653289e20252592d0aab6680948f36eb5b4362d403fef09170 https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q3_K_M.gguf /app/models/qwen3-1.7b-q3_k_m.gguf
ENV LOCAL_MODEL_PATH=/app/models/qwen3-1.7b-q3_k_m.gguf \
    LOCAL_MODEL_NAME=qwen3-1.7b-q3_k_m \
    LOCAL_MODEL_CONTEXT=1536 \
    LOCAL_MODEL_THREADS=2
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
