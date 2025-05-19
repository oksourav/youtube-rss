#!/bin/sh

# Production startup script for Render (Alpine compatible)
PORT=${PORT:-5000}

# Use Gunicorn in production for better performance and security
if [ "$RENDER" = "true" ]; then
    echo "Starting with Gunicorn for production..."
    exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60 --preload youtube_rss_filter:app
else
    echo "Starting with Flask development server..."
    exec python youtube_rss_filter.py
fi
