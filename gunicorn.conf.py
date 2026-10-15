# Gunicorn reads this file automatically, so the Render Start Command can stay "gunicorn app:app".
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers = 1
threads = 8
timeout = 120          # never kill a request that is still waiting for the AI models
graceful_timeout = 30
