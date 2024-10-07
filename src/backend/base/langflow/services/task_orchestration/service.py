# langflow/orchestrator/task_orchestrator.py

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from diskcache import Cache, Deque
from loguru import logger
from pydantic import BaseModel, field_validator
from sqlmodel import select

from langflow.services.base import Service
from langflow.services.database.models.subscription.model import Subscription
from langflow.services.database.models.task.model import (  # removed TaskStatus import
    Task,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)

if TYPE_CHECKING:
    from langflow.services.database.service import DatabaseService
    from langflow.services.settings.service import SettingsService


class TaskNotification(BaseModel):
    task_id: str
    flow_id: str
    event_type: str
    category: str
    state: str
    status: str

    @field_validator("task_id", "flow_id", mode="before")
    @classmethod
    def validate_str(cls, value: str) -> str:
        try:
            return str(value)
        except ValueError as exc:
            msg = f"Invalid UUID: {value}"
            raise ValueError(msg) from exc


def add_tasks_to_database_url(database_url: str) -> str:
    # Add a mention that this is a different db for tasks
    if database_url.startswith("sqlite://"):
        # For SQLite, append "-tasks" before the file extension
        parts = database_url.rsplit(".", 1)
        return f"{parts[0]}-tasks.{parts[1]}" if len(parts) > 1 else f"{database_url}-tasks"
    if database_url.startswith(("postgresql://", "mysql://")):
        # For PostgreSQL and MySQL, append "_tasks" to the database name
        if "?" in database_url:
            base_url, params = database_url.split("?", 1)
            return f"{base_url}_tasks?{params}"
        return f"{database_url}_tasks"
    # For unsupported database types, return the original URL
    return database_url


class TaskOrchestrationService(Service):
    name = "task_orchestration_service"

    def __init__(
        self,
        settings_service: "SettingsService",
        db_service: "DatabaseService",
    ):
        cache_dir = Path(settings_service.settings.config_dir) / "task_orchestrator"
        self.cache = Cache(cache_dir)
        self.notification_queue = Deque(directory=f"{cache_dir}/notifications")
        self.db: DatabaseService = db_service
        database_url = add_tasks_to_database_url(self.db.database_url)
        jobstores = {"default": SQLAlchemyJobStore(url=database_url)}

        # Initialize job scheduler
        self.scheduler = AsyncIOScheduler(jobstores=jobstores)

    async def start(self):
        self.scheduler.start()

    async def stop(self):
        self.scheduler.shutdown()

    def create_task(self, task_create: TaskCreate) -> TaskRead:
        task = Task.model_validate(task_create, from_attributes=True)
        task.status = "pending"  # set status as string
        with self.db.with_session() as session:
            session.add(task)
            session.commit()
            session.refresh(task)

        task_read = TaskRead.model_validate(task, from_attributes=True)
        self._notify(task_read, "task_created")
        self._schedule_task(task_read)
        return task_read

    def update_task(self, task_id: UUID | str, task_update: TaskUpdate) -> TaskRead:
        with self.db.with_session() as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if not task:
                msg = f"Task with id {task_id} not found"
                raise ValueError(msg)

            for key, value in task_update.model_dump(exclude_unset=True).items():
                if key == "status":
                    setattr(task, key, value)  # set status as string
                else:
                    setattr(task, key, value)

                session.commit()
                session.refresh(task)

        task_read = TaskRead.model_validate(task, from_attributes=True)
        self._notify(task_read, "task_updated")
        return task_read

    def get_task(self, task_id: str) -> TaskRead:
        with self.db.with_session() as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if not task:
                msg = f"Task with id {task_id} not found"
                raise ValueError(msg)
        return TaskRead.model_validate(task, from_attributes=True)

    def get_tasks_for_flow(self, flow_id: str) -> list[TaskRead]:
        with self.db.with_session() as session:
            tasks = session.exec(select(Task).where(Task.flow_id == flow_id)).all()
        return [TaskRead.model_validate(task, from_attributes=True) for task in tasks]

    def delete_task(self, task_id: str) -> None:
        with self.db.with_session() as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if not task:
                msg = f"Task with id {task_id} not found"
                raise ValueError(msg)
            session.delete(task)
            session.commit()

    def _notify(self, task: TaskRead, event_type: str) -> None:
        # Notify author
        self._add_notification(task, event_type, task.author_id)

        # Notify assignee
        self._add_notification(task, event_type, task.assignee_id)

        # Notify subscribers
        with self.db.with_session() as session:
            subscriptions = session.exec(
                select(Subscription).filter(
                    ((Subscription.category == task.category) & (Subscription.state == task.state))
                    | ((Subscription.category.is_(None)) & (Subscription.state.is_(None)))
                )
            ).all()

        for subscription in subscriptions:
            self._add_notification(task, event_type, subscription.flow_id)

    def _add_notification(self, task: TaskRead, event_type: str, flow_id: str) -> None:
        notification = TaskNotification(
            task_id=task.id,
            flow_id=flow_id,
            event_type=event_type,
            category=task.category,
            state=task.state,
            status=task.status,
        )
        self.notification_queue.append(notification.model_dump())

    def get_notifications(self) -> list[TaskNotification]:
        notifications = []
        while self.notification_queue:
            notifications.append(TaskNotification(**self.notification_queue.popleft()))
        return notifications

    def subscribe_flow(
        self, flow_id: str, event_type: str, category: str | None = None, state: str | None = None
    ) -> None:
        subscription = Subscription(flow_id=flow_id, event_type=event_type, category=category, state=state)
        with self.db.with_session() as session:
            session.add(subscription)
            session.commit()

    def unsubscribe_flow(
        self, flow_id: str, event_type: str, category: str | None = None, state: str | None = None
    ) -> None:
        with self.db.with_session() as session:
            query = session.exec(
                select(Subscription).where(Subscription.flow_id == flow_id, Subscription.event_type == event_type)
            )
            if category:
                query = query.filter(Subscription.category == category)
            if state:
                query = query.filter(Subscription.state == state)
            query.delete()
            session.commit()

    def _schedule_task(self, task: TaskRead) -> None:
        if task.cron_expression:
            trigger = CronTrigger.from_crontab(task.cron_expression)
            self.scheduler.add_job(
                self.consume_task, trigger=trigger, args=[task.id], id=str(task.id), replace_existing=True
            )
        else:
            self.scheduler.add_job(self.consume_task, args=[task.id], id=str(task.id), replace_existing=True)

    async def consume_task(self, task_id: str | UUID) -> None:
        task = self.get_task(str(task_id))
        if task.status != "pending":
            msg = f"Task {task_id} is not in pending status"
            raise ValueError(msg)

        # Update task status to "processing"
        self.update_task(task_id, TaskUpdate(status="processing", state=task.state))

        try:
            # Perform task processing logic here
            result = self._process_task(task)

            # Update task with result and set status to "completed"
            self.update_task(task_id, TaskUpdate(status="completed", state=task.state, result=result))
        except Exception as e:
            # If an error occurs, update task status to "failed"
            self.update_task(task_id, TaskUpdate(status="failed", state=task.state, result={"error": str(e)}))

    def _process_task(self, task: TaskRead) -> dict:
        # Implement task processing logic based on task category
        # This is a placeholder implementation
        logger.info(f"Processing task {task.id}")
        return {"result": "Task processed successfully"}
