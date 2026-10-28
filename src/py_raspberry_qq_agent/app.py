from __future__ import annotations

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .agent import AgentRunner, AgentWorker
from .allowlist import ChatAllowlist
from .assignments import AssignmentNotifications
from .config import AppConfig
from .contacts import ContactDirectory, RoleStore
from .digest import GroupDigestService
from .guild import DueReminders, GuildRepository, WindowPoller
from .guild_reports import WeeklyReports
from .incoming import IncomingService
from .keycloak import KeycloakDirectory
from .knowledge import KnowledgeBase
from .llm import OpenAICompatibleClient
from .logging_setup import configure_logging
from .mcp_runtime import McpManager
from .messages import MessageStore
from .napcat import IncomingMessage, NapCatClient, OutboundQueue
from .priority import ImportanceGate
from .scheduler import ScheduledTask, Scheduler, ScheduleStore
from .tasks import TaskQueue
from .toolset import ChatDirectoryService, HistoryService, build_tool_registry

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Application:
    config: AppConfig
    roles: RoleStore
    contacts: ContactDirectory
    tasks: TaskQueue
    schedules: ScheduleStore
    messages: MessageStore
    knowledge: KnowledgeBase
    main_llm: OpenAICompatibleClient
    classifier_llm: OpenAICompatibleClient
    mcp: McpManager
    napcat: NapCatClient
    outbound: OutboundQueue
    allowlist: ChatAllowlist
    history: HistoryService
    chat_directory: ChatDirectoryService
    incoming: IncomingService
    digest: GroupDigestService
    worker: AgentWorker | None = None
    scheduler: Scheduler | None = None
    identities: KeycloakDirectory | None = None
    due_poller: WindowPoller | None = None
    assignment_poller: WindowPoller | None = None
    weekly_reports: WeeklyReports | None = None

    @classmethod
    def build(cls, config: AppConfig) -> Application:
        config.prepare_directories()
        configure_logging(config)

        roles = RoleStore(config.data_dir / "roles.json")
        for raw_admin_id in config.admin_qq_ids:
            roles.set_admin(raw_admin_id, True)
        contacts = ContactDirectory(config.data_dir / "profiles.json")
        tasks = TaskQueue()
        schedules = ScheduleStore(config.data_dir / "schedules.json", config.timezone)
        messages = MessageStore(
            config.data_dir / "messages.jsonl",
            max_fetch=config.history_max_fetch,
            max_content_chars=config.history_content_chars,
        )
        knowledge = KnowledgeBase(
            config.knowledge_dir,
            config.notebook_dir,
            max_file_bytes=config.knowledge_max_file_bytes,
            max_results=config.knowledge_max_search_results,
            max_read_lines=config.knowledge_max_read_lines,
        )
        main_llm = OpenAICompatibleClient(
            config.main_llm, retry_count=config.main_llm.retry_count
        )
        classifier_llm = OpenAICompatibleClient(
            config.classifier_llm, retry_count=config.classifier_llm.retry_count
        )
        mcp = McpManager(config)
        allowlist = ChatAllowlist(
            enabled=config.allowlist_enabled,
            private_qq_ids=config.allowlist_private_qq_ids,
            group_ids=config.allowlist_group_ids,
        )
        if config.allowlist_enabled:
            LOGGER.info(
                "chat allowlist enabled private=%d groups=%d",
                len(config.allowlist_private_qq_ids),
                len(config.allowlist_group_ids),
            )
            if not (config.allowlist_private_qq_ids or config.allowlist_group_ids):
                LOGGER.warning(
                    "chat allowlist is enabled but empty; all chat triggers and "
                    "model-initiated sends will be blocked"
                )

        incoming: IncomingService

        async def on_napcat_message(message: IncomingMessage) -> None:
            await incoming.handle(message)

        napcat = NapCatClient(config, on_napcat_message)
        outbound = OutboundQueue(config, napcat, allowlist, messages)
        gate = ImportanceGate(
            config, classifier_llm, allowlist, roles, contacts, messages
        )
        history = HistoryService(config, messages, napcat)
        chat_directory = ChatDirectoryService(config, napcat)
        incoming = IncomingService(
            contacts=contacts,
            messages=messages,
            gate=gate,
            tasks=tasks,
            dedupe_messages=config.history_dedupe_messages,
        )
        digest = GroupDigestService(
            config=config,
            history=history,
            llm=main_llm,
            knowledge=knowledge,
            mcp=mcp,
        )
        application = cls(
            config=config,
            roles=roles,
            contacts=contacts,
            tasks=tasks,
            schedules=schedules,
            messages=messages,
            knowledge=knowledge,
            main_llm=main_llm,
            classifier_llm=classifier_llm,
            mcp=mcp,
            napcat=napcat,
            outbound=outbound,
            allowlist=allowlist,
            history=history,
            chat_directory=chat_directory,
            incoming=incoming,
            digest=digest,
        )
        return application

    async def start(self) -> None:
        await self.mcp.start()
        registry = build_tool_registry(
            config=self.config,
            schedules=self.schedules,
            knowledge=self.knowledge,
            contacts=self.contacts,
            outbound=self.outbound,
            history=self.history,
            chat_directory=self.chat_directory,
            mcp=self.mcp,
        )
        self.worker = AgentWorker(
            AgentRunner(self.config, self.main_llm, registry),
            self.tasks,
        )
        self.scheduler = Scheduler(
            self.schedules,
            self._enqueue_scheduled_task,
            self.config.scheduler_tick_seconds,
        )
        self.outbound.start()
        self.worker.start()
        if self.config.scheduler_enabled:
            self.scheduler.start()
        self.digest.start()
        self.napcat.start()
        if self.config.guild.enabled:
            self.identities = KeycloakDirectory(self.config.keycloak)
            repository = GuildRepository(self.config.guild.database_url)
            self.weekly_reports = WeeklyReports(
                self.config,
                repository,
                self.identities,
                self.main_llm,
                self.knowledge,
                self.mcp,
            )
            self.weekly_reports.start()
            if self.config.guild.assignments_enabled:
                assignments = AssignmentNotifications(
                    self.config.guild, repository, self.identities, self.outbound
                )
                self.assignment_poller = WindowPoller(
                    repository,
                    self.config.guild.poll_seconds,
                    assignments.check,
                    "guild-assignments",
                )
                self.assignment_poller.start()
            if self.config.guild.due_enabled:
                reminders = DueReminders(
                    self.config.guild,
                    repository,
                    self.identities,
                    self.outbound,
                    ZoneInfo(self.config.timezone),
                )
                self.due_poller = WindowPoller(
                    repository,
                    self.config.guild.poll_seconds,
                    reminders.check,
                    "guild-due",
                )
                self.due_poller.start()
        LOGGER.info(
            "application started timezone=%s tools=%d",
            self.config.timezone,
            len(registry.definitions()),
        )

    async def _enqueue_scheduled_task(self, task: ScheduledTask) -> None:
        if self.worker is None:
            raise RuntimeError("agent worker is not started")
        item = await self.tasks.add(
            content=f"处理定时任务：{task.content}",
            source="scheduler",
            context=(f"标题：{task.title}\n任务：{task.content}\n"),
            chat_type=task.chat_type,
            target_id=task.target_id,
            metadata={"kind": "scheduled_task", "task_id": task.id},
        )
        LOGGER.info("scheduled agent task queued id=%s schedule=%s", item.id, task.id)

    async def stop(self) -> None:
        LOGGER.info("application stopping")
        if self.due_poller:
            await self.due_poller.stop()
        if self.assignment_poller:
            await self.assignment_poller.stop()
        if self.weekly_reports:
            await self.weekly_reports.stop()
        if self.identities:
            await self.identities.close()
        await self.napcat.stop()
        await self.digest.stop()
        if self.scheduler is not None and self.config.scheduler_enabled:
            await self.scheduler.stop()
        await self.outbound.stop()
        if self.worker is not None:
            await self.worker.stop()
        await self.mcp.stop()
        await self.main_llm.close()
        await self.classifier_llm.close()
        LOGGER.info("application stopped")
