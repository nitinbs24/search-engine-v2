FROM python:3.11-slim
WORKDIR /app
# shortcut: CPU-only torch keeps the image small; the GPU build is added with the GPU opt-in (T22).
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
