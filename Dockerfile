# Use a lightweight official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose the dynamic port
EXPOSE 8000

# Run FastAPI app (shell expands $PORT from Railway)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
