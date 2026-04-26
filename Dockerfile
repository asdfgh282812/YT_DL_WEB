FROM python:3.11-slim

# Install ffmpeg and other dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create volume mount points
RUN mkdir -p /app/temp /app/config /app/frontend

# Expose port
EXPOSE 8000

# Set Python to run unbuffered
ENV PYTHONUNBUFFERED=1

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
