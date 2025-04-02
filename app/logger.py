import sys
from datetime import datetime
from typing import Dict, Optional

from loguru import logger as _logger

from app.config import PROJECT_ROOT


class Logger:
    _instances: Dict[str, "Logger"] = {}
    
    def __new__(cls, logger_name: str = "default"):
        if logger_name not in cls._instances:
            instance = super().__new__(cls)
            instance.__init__(logger_name)
            cls._instances[logger_name] = instance
        return cls._instances[logger_name]
    
    def __init__(self, logger_name: str = "default"):
        if not hasattr(self, "_logger"):  # 仅在尚未初始化时进行初始化
            self._logger = _logger
            self._print_level = "INFO"
            self._logger_name = logger_name
            
            # 配置默认日志
            self._logger.remove()
            self._logger.add(sys.stderr, level=self._print_level)
            
            current_date = datetime.now()
            formatted_date = current_date.strftime("%Y%m%d%H%M%S")
            log_name = f"{logger_name}_{formatted_date}" if logger_name != "default" else formatted_date
            
            self._logger.add(PROJECT_ROOT / f"logs/{log_name}.log", level="DEBUG")
    
    def define_log_level(self, print_level: str = "INFO", logfile_level: str = "DEBUG", name: Optional[str] = None):
        """调整日志级别并返回日志记录器实例"""
        self._print_level = print_level
        
        current_date = datetime.now()
        formatted_date = current_date.strftime("%Y%m%d%H%M%S")
        log_name = f"{name}_{formatted_date}" if name else formatted_date
        
        self._logger.remove()
        self._logger.add(sys.stderr, level=print_level)
        self._logger.add(PROJECT_ROOT / f"logs/{log_name}.log", level=logfile_level)
        
        return self._logger
    
    def info(self, message):
        self._logger.info(message)
        
    def debug(self, message):
        self._logger.debug(message)
        
    def warning(self, message):
        self._logger.warning(message)
        
    def error(self, message):
        self._logger.error(message)
        
    def critical(self, message):
        self._logger.critical(message)
        
    def exception(self, message):
        self._logger.exception(message)


# 创建默认日志实例
logger = Logger()


# 为兼容现有代码，保留define_log_level函数
def define_log_level(print_level="INFO", logfile_level="DEBUG", name: str = None):
    """兼容原有函数，调整日志级别并返回日志记录器实例"""
    return logger.define_log_level(print_level, logfile_level, name)


if __name__ == "__main__":
    logger.info("Starting application")
    logger.debug("Debug message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")
    
    try:
        raise ValueError("Test error")
    except Exception as e:
        logger.exception(f"An error occurred: {e}")
        
    # 测试单例模式
    logger2 = Logger()
    logger2.info("这应该使用同一个实例")
    
    # 测试创建新的命名实例
    custom_logger = Logger("custom")
    custom_logger.info("这是一个新的命名实例")
