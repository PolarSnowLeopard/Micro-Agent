#!/bin/bash
service ssh start
uvicorn api.app:app --host 0.0.0.0 --port 8010
