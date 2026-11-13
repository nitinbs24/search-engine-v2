FROM python:3.11-slim
WORKDIR /app
# CPU-only torch by default keeps the image small; the GPU opt-in (GPU=1) passes the CUDA index.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch==2.5.1 --index-url $TORCH_INDEX
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
