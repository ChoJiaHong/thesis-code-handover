import time
import logging

def timing_decorator(label):
    def decorator(func):
        def wrapper(*args, **kwargs):
            logger = logging.getLogger("timing")
            start = time.time()
            result = func(*args, **kwargs)
            end = time.time()
            logger.debug("%s: %.2f ms", label, (end - start)*1000)
            return result
        return wrapper
    return decorator
