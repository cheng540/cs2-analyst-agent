"""日志：控制台 + 按天滚动文件，UTF-8 编码。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_initialized = False


def _fix_console_encoding() -> None:
    """Windows 控制台默认 GBK，无法输出 emoji/¥ 等字符：切换为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """初始化全局日志（幂等，重复调用无副作用）。"""
    global _initialized
    if _initialized:
        return
    _fix_console_encoding()
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_file = log_dir / f"agent_{datetime.now():%Y-%m-%d}.log"
    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 降低第三方库噪音
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
