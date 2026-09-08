FROM python:3.12-slim AS runtime
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
RUN mkdir -p /app/models
ADD --checksum=sha256:74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf /app/models/qwen2.5-0.5b-instruct-q4_k_m.gguf
ENV LOCAL_MODEL_PATH=/app/models/qwen2.5-0.5b-instruct-q4_k_m.gguf \
    LOCAL_MODEL_NAME=qwen2.5-0.5b-instruct-q4_k_m \
    LOCAL_MODEL_CONTEXT=1536 \
    LOCAL_MODEL_THREADS=2 \
    LOCAL_MODEL_CHAT_FORMAT=chatml
EXPOSE 8080
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
