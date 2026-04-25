"""Logging configuration for CloudHealth."""
import logging
import os
from datetime import datetime
from typing import Tuple

def setup_logger(output_dir: str) -> Tuple[logging.Logger, str]:
    """
    Sets up a logger that writes to both a file and the console.
    
    Args:
        output_dir: Directory where the log file will be created.
        
    Returns:
        A tuple of (Logger instance, log file path).
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    log_filename = f"healthcheck_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path = os.path.join(output_dir, log_filename)
    
    logger = logging.getLogger("HealthCheck")
    logger.setLevel(logging.DEBUG)
    
    # File handler for detailed logs
    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    # Console handler for summary
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(ch)
    
    return logger, log_path

class CommandLogger:
    """Helper class to log remote command execution details."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        
    def log_command(self, target: str, command: str, output: str, exit_code: int):
        """Logs the command, its exit code, and the raw output to debug level."""
        self.logger.debug(f"[{target}] COMMAND: {command}")
        self.logger.debug(f"[{target}] EXIT CODE: {exit_code}")
        self.logger.debug(f"[{target}] OUTPUT:\n{output}\n{'-'*40}")

