# Use Python Alpine for maximum security (0 critical, 0 high vulnerabilities)
FROM python:3.13-alpine

# Set working directory
WORKDIR /app

# Install required packages and security updates
RUN apk update && \
    apk upgrade && \
    apk add --no-cache curl && \
    rm -rf /var/cache/apk/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies with security considerations
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY youtube_rss_filter.py start.sh ./

# Make start script executable
RUN chmod +x start.sh

# Create non-root user for security
RUN adduser -D -s /bin/sh -u 1001 app && \
    chown -R app:app /app
USER app

# Set environment variables for security
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV RENDER=true

# Expose port (Render will set the PORT env var)
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:${PORT:-5000}/health || exit 1

# Run the application using the startup script
CMD ["./start.sh"]
