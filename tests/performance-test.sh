#!/bin/bash

# Ensure wrk is installed and accessible
if ! command -v wrk &> /dev/null
then
    echo "wrk could not be found, please install it first."
    exit
fi

# Define the URL and script path
URL="https://127.0.0.1/api/v1/scan"
SCRIPT_PATH="$(dirname "$0")/upload.lua"

# Check if the upload.lua script exists
if [ ! -f "$SCRIPT_PATH" ]; then
    echo "The script $SCRIPT_PATH does not exist."
    exit 1
fi

# Run the wrk performance test
if ! wrk -t12 -c400 -d30s --timeout 10s -s "$SCRIPT_PATH" "$URL"; then
    echo "Failed to execute wrk command."
    exit 1
fi
