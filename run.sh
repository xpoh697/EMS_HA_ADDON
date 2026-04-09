#!/usr/bin/with-contenv bashio

# Get options from Home Assistant
LOG_LEVEL=$(bashio::config 'log_level')
DRY_RUN=$(bashio::config 'dry_run')

bashio::log.info "Starting Energy Management System..."
bashio::log.info "Log Level: ${LOG_LEVEL}"
bashio::log.info "Dry Run: ${DRY_RUN}"

# Start the FastAPI application
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
