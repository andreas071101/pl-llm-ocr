#!/bin/sh
echo "192.168.30.142 b3078d17-andreas4007-olares.local 059cd05d-andreas4007-olares.local" >> /etc/hosts
exec uvicorn api:app --host 0.0.0.0 --port 8000
