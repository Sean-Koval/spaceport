"""Allowlists process metadata; never renders arbitrary messages or exceptions."""
import json
import logging
import re
import time

EVENTS = frozenset({'runtime_event','started','stopped','operation_failed','maintenance_complete'})


class SafeFormatter(logging.Formatter):
    def format(self, record):
        event = getattr(record,'event','runtime_event')
        if type(event) is not str or event not in EVENTS: event = 'runtime_event'
        return json.dumps({'time':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(record.created)),
            'level':record.levelname if type(record.levelname) is str and record.levelname in {'DEBUG','INFO','WARNING','ERROR','CRITICAL'} else 'INFO',
            'event':event},separators=(',',':'))


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.raiseExceptions = False
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ('uvicorn','uvicorn.error','uvicorn.access','httpx','httpcore','sqlalchemy','alembic'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').disabled = True
