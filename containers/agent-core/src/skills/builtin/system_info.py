import psutil

from ..base import SkillBase
from ..types import SkillDefinition, SkillResult


class SystemInfoSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="system_info",
            description="Get current CPU, RAM, and disk usage.",
            params=[],
            category="system",
            timeout_seconds=5.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            output = (
                f"CPU: {cpu_percent}% ({psutil.cpu_count()} cores)\n"
                f"RAM: {mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB ({mem.percent}%)\n"
                f"Disk: {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB ({disk.percent}%)\n"
                f"Load avg (1/5/15m): {', '.join(f'{x:.2f}' for x in psutil.getloadavg())}"
            )
            return SkillResult(skill_name="system_info", success=True, output=output)
        except Exception as e:
            return SkillResult(skill_name="system_info", success=False, output="", error=str(e))
