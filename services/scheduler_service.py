"""Scheduler service using QStash for delayed tasks."""
import os
import time
from typing import Optional, Dict
from qstash import QStash

from storage.models import get_task, save_task, delete_task, acquire_task_lock, release_task_lock


def _get_qstash_client() -> QStash:
    """Get QStash client from environment."""
    token = os.getenv("QSTASH_TOKEN")
    if not token:
        raise RuntimeError("QSTASH_TOKEN не найден")
    return QStash(token=token)


class SchedulerService:
    """Service for scheduling and processing tasks."""
    
    def __init__(self):
        self._qstash: Optional[QStash] = None
        self._app_url: str = ""
    
    def initialize(self, app_url: str):
        """Initialize with app URL."""
        self._app_url = app_url.rstrip("/")
        self._qstash = _get_qstash_client()
    
    @property
    def qstash(self) -> QStash:
        if self._qstash is None:
            self._qstash = _get_qstash_client()
        return self._qstash
    
    def schedule_task(self, task_id: str, delay_minutes: int = 1) -> bool:
        """Schedule a task to be processed after delay."""
        if not self._app_url:
            raise RuntimeError("App URL not configured. Call initialize() first.")
        
        target_url = f"{self._app_url}/api/process"
        
        try:
            delay_str = f"{delay_minutes}m"
            self.qstash.message.publish_json(
                url=target_url,
                body={"task_id": task_id},
                delay=delay_str
            )
            return True
        except Exception as e:
            print(f"Ошибка планирования задачи {task_id}: {e}")
            return False
    
    def process_task(self, task_id: str) -> Dict:
        """Process a scheduled task."""
        # Acquire lock to prevent duplicate processing
        if not acquire_task_lock(task_id, ttl=120):
            return {"ok": False, "status": "already_processing"}
        
        try:
            task = get_task(task_id)
            if not task or task.get("status") not in ("active", "pending"):
                release_task_lock(task_id)
                return {"ok": False, "error": "Task not found or inactive"}
            
            # Import here to avoid circular imports
            from services.telegram_service import TelegramService
            
            service = TelegramService()
            success_count = 0
            error_count = 0
            
            try:
                await service.connect()
                for gid in task.get("groups", []):
                    ok = await service.send_message(int(gid), task.get("message"))
                    if ok:
                        success_count += 1
                    else:
                        error_count += 1
            except Exception as e:
                print(f"Ошибка отправки: {e}")
                task["status"] = "failed"
                save_task(task)
                release_task_lock(task_id)
                return {"ok": False, "error": str(e)}
            finally:
                await service.disconnect()
            
            # Update task status
            task["completed_repeats"] = task.get("completed_repeats", 0) + 1
            
            if task["completed_repeats"] >= task.get("total_repeats", 1):
                task["status"] = "completed"
            else:
                # Schedule next run
                task["next_run"] = time.time() + task.get("interval_minutes", 1) * 60
                task["status"] = "active"
                save_task(task)
                # Reschedule
                self.schedule_task(task_id, delay_minutes=task.get("interval_minutes", 1))
                release_task_lock(task_id)
                return {"ok": True, "sent": success_count, "errors": error_count, "rescheduled": True}
            
            save_task(task)
            release_task_lock(task_id)
            return {"ok": True, "sent": success_count, "errors": error_count}
            
        except Exception as e:
            print(f"Ошибка обработки задачи {task_id}: {e}")
            release_task_lock(task_id)
            return {"ok": False, "error": str(e)}


# Convenience functions
_scheduler: Optional[SchedulerService] = None


def get_scheduler(app_url: str = None) -> SchedulerService:
    """Get or create scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
        if app_url:
            _scheduler.initialize(app_url)
    return _scheduler


def schedule_task(task_id: str, delay_minutes: int = 1) -> bool:
    """Schedule a task."""
    scheduler = get_scheduler()
    return scheduler.schedule_task(task_id, delay_minutes)


async def process_scheduled_task(task_id: str) -> Dict:
    """Process a scheduled task."""
    scheduler = get_scheduler()
    return scheduler.process_task(task_id)
