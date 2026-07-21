"""日志模块"""
import logging
import sys
import os
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

CONSOLE_LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()

_log_file = LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_file_handler = None


def get_logger(name: str) -> logging.Logger:
    global _file_handler

    logger = logging.getLogger(name)

    if not logger.handlers:
        logger.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, CONSOLE_LOG_LEVEL, logging.WARNING))
        console_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
        logger.addHandler(console_handler)

        if _file_handler is None:
            _file_handler = logging.FileHandler(_log_file, encoding='utf-8')
            _file_handler.setLevel(logging.DEBUG)
            _file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

        logger.addHandler(_file_handler)

    return logger
