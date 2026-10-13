"""Entry point: start the dashboard (and the background scheduler) on a single process."""

import atexit

import config
from app import create_app
from config import setup_logging
from scheduler import start_scheduler

setup_logging()
app = create_app()

if __name__ == "__main__":
    if config.SCHEDULER_ENABLED:
        sched = start_scheduler()
        if sched is not None:
            atexit.register(lambda: sched.shutdown(wait=False))
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)
