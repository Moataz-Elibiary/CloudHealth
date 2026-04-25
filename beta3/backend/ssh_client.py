import asyncio
import os
from dataclasses import dataclass

@dataclass
class SSHResult:
    stdout: str
    stderr: str
    exit_code: int
    
    @property
    def out(self): return self.stdout

class SSHClient:
    """Standardized Async SSH client for node-level audits."""
    def __init__(self, host: str, user: str, pwd: str = None, key_path: str = None, timeout: int = 30):
        self.host = host
        self.user = user
        self.pwd = pwd
        self.key_path = key_path
        self.timeout = timeout

    async def connect(self):
        return None

    async def close(self):
        return None

    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        # For the Beta 3 Bastion-native execution, we use LocalClient logic 
        # but keep the SSHClient interface for compatibility with existing checks.
        import subprocess
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return SSHResult(stdout.decode(), stderr.decode(), proc.returncode)
        except asyncio.TimeoutError:
            return SSHResult("", "Command Timed Out", -1)
        except Exception as e:
            return SSHResult("", str(e), -1)

class LocalClient(SSHClient):
    """Executes commands directly on the current machine (Bastion)."""
    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        return await super().run(cmd, timeout)
