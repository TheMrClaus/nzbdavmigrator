FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy application files
COPY export_nzb.py .
COPY nzbdav_web.py .
COPY start_web.py .
COPY entrypoint.sh .

# Make entrypoint executable
RUN chmod +x entrypoint.sh

# Create a non-root user first
ARG USER_ID=1000
ARG GROUP_ID=1000
RUN groupadd -g ${GROUP_ID} nzbuser && useradd -m -u ${USER_ID} -g ${GROUP_ID} nzbuser

# Create data directory for database and config with proper ownership
RUN mkdir -p /app/data && chown -R nzbuser:nzbuser /app && chmod 755 /app/data

# Switch to non-root user (comment out to run as root)
USER nzbuser

# Expose port
EXPOSE 9999

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9999/api/status', timeout=5)"

# Start the web application
CMD ["./entrypoint.sh"]