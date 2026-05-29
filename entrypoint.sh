#!/bin/sh
echo "192.168.30.142 b3078d17-andreas4007-olares.local 059cd05d-andreas4007-olares.local" >> /etc/hosts

echo "--- Configuration ---"
echo "PAPERLESS_URL:   ${PAPERLESS_URL}"
echo "VISION_API_URL:  ${VISION_API_URL}"
echo "VISION_MODEL:    ${VISION_MODEL}"
echo "VISION_API_KEY:  ${VISION_API_KEY:+set}"
echo "---------------------"

exec uvicorn api:app --host 0.0.0.0 --port 8000
