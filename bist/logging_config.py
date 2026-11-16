import logging
import time


def configure_logging(level=logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class StepTimer:
    """Log the time each step took since the previous mark, without re-indenting code."""

    def __init__(self, logger, scope):
        self.logger, self.scope = logger, scope
        self.last = time.perf_counter()

    def mark(self, step):
        now = time.perf_counter()
        self.logger.info("%s: %s took %.1f seconds", self.scope, step, now - self.last)
        self.last = now

