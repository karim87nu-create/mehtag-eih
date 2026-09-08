FROM python:3.12-slim AS runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
COPY . .
RUN mkdir -p /app/models
ADD --checksum=sha256:58cb5c05ecef48e82961f1a2be6544145ea26136f69dddda4bbbd092f0e4b993 https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q3_k_m.gguf /app/models/qwen2.5-1.5b-instruct-q3_k_m.gguf
ENV LOCAL_MODEL_PATH=/app/models/qwen2.5-1.5b-instruct-q3_k_m.gguf \
    LOCAL_MODEL_NAME=qwen2.5-1.5b-instruct-q3_k_m \
    LOCAL_MODEL_CONTEXT=1536 \
    LOCAL_MODEL_THREADS=2
EXPOSE 8080
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
