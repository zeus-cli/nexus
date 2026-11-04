"""Monitoramento de recursos do sistema para estabilidade do NEXUS."""

import psutil
import os
from typing import Any

class ResourceManager:
    """Monitora CPU, RAM e Swap para prevenir travamentos em hardware limitado."""

    def __init__(self, ram_threshold_mb: int = 500, swap_threshold_percent: float = 80.0):
        self._ram_threshold_mb = ram_threshold_mb
        self._swap_threshold_percent = swap_threshold_percent

    def get_stats(self) -> dict[str, Any]:
        """Retorna a telemetria atual do sistema."""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        return {
            "ram": {
                "total_gb": round(mem.total / (1024**3), 2),
                "used_gb": round(mem.used / (1024**3), 2),
                "available_mb": round(mem.available / (1024**2), 2),
                "percent": mem.percent,
                "status": "healthy" if mem.available / (1024**2) > self._ram_threshold_mb else "critical",
            },
            "cpu": {
                "percent": psutil.cpu_percent(interval=None),
                "cores": psutil.cpu_count(logical=True),
                "load_avg": psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0,0,0),
            },
            "swap": {
                "total_gb": round(swap.total / (1024**3), 2),
                "used_gb": round(swap.used / (1024**3), 2),
                "percent": swap.percent,
                "status": "healthy" if swap.percent < self._swap_threshold_percent else "critical",
            },
            "pressure": self.check_pressure(),
        }

    def check_pressure(self) -> str:
        """Avalia se o sistema está sob pressão crítica de recursos."""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        if mem.available / (1024**2) < self._ram_threshold_mb:
            return "CRITICAL_RAM"
        if swap.percent > self._swap_threshold_percent:
            return "CRITICAL_SWAP"
        return "OK"
