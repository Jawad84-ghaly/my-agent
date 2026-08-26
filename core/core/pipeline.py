"""Le pipeline complet : d'un message entrant à une action accomplie.

Ordre de traitement, et pourquoi il est dans cet ordre :

1. **Reprise de validation d'abord.** Si un « ok » est attendu sur ce fil, il
   doit être interprété comme tel — pas replanifié comme une nouvelle demande.
   Inverser ces deux étapes fait qu'un « ok » relance une planification vide.
2. **Transcription.** Un vocal mal transcrit vaut mieux refusé que deviné : sous
   le seuil de confiance, on demande de répéter plutôt que d'agir à l'aveugle.
3. **Routage.** Les requêtes triviales n'ont pas besoin du gros modèle.
4. **Planification, exécution, gate, réponse.**

Les dépendances lourdes — modèle de langage, transcription, envoi — sont des
protocoles injectés. Le pipeline se teste donc entièrement hors ligne.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .approvals import ApprovalRegistry, PendingApproval
from .gate import classify_reply, format_approval_summary
from .graph.executor import ExecutionState, execute_plan
from .messaging import ChannelFormatter, Sender
from .planning import PlanError, Task
from .tools.registry import ToolRegistry

#: En dessous, la transcription n'est pas exploitable.
MIN_TRANSCRIPT_CONFIDENCE = 0.6


@dataclass
class IncomingMessage:
    id: str
    user_id: str
    thread_id: str
    channel: str
    from_id: str
    kind: str = "text"  # text | audio | image
    text: str | None = None
    media_url: str | None = None


class Transcriber(Protocol):
    async def transcribe(self, media_url: str) -> tuple[str, float]: ...


class Router(Protocol):
    async def route(self, text: str, context: dict) -> dict: ...


class Planner(Protocol):
    async def plan(self, text: str, context: dict) -> list[Task]: ...


class Responder(Protocol):
    async def summarize(self, text: str, state: ExecutionState, context: dict) -> str: ...


@dataclass
class Pipeline:
    tools: ToolRegistry
    router: Router
    planner: Planner
    responder: Responder
    sender: Sender
    approvals: ApprovalRegistry = field(default_factory=ApprovalRegistry)
    transcriber: Transcriber | None = None

    async def handle(self, message: IncomingMessage, now: datetime | None = None) -> str:
        """Traite un message et renvoie ce qui a été envoyé à l'utilisateur."""
        now = now or datetime.now(timezone.utc)
        formatter = ChannelFormatter(message.channel)

        text = await self._resolve_text(message)
        if text is None:
            return await self._reply(message, formatter, "🎤 Je n'ai pas bien saisi, tu peux répéter ?")

        pending = self.approvals.get(message.thread_id, now)
        if pending is not None:
            return await self._resume(message, formatter, pending, text, now)

        return await self._fresh_request(message, formatter, text, now)

    # --- étapes ----------------------------------------------------------

    async def _resolve_text(self, message: IncomingMessage) -> str | None:
        if message.kind != "audio":
            return (message.text or "").strip() or None
        if self.transcriber is None or not message.media_url:
            return None
        transcript, confidence = await self.transcriber.transcribe(message.media_url)
        if confidence < MIN_TRANSCRIPT_CONFIDENCE or not transcript.strip():
            return None
        return transcript.strip()

    async def _resume(
        self,
        message: IncomingMessage,
        formatter: ChannelFormatter,
        pending: PendingApproval,
        text: str,
        now: datetime,
    ) -> str:
        decision = classify_reply(text)

        if decision == "reject":
            self.approvals.pop(message.thread_id, now)
            return await self._reply(message, formatter, "OK, annulé.")

        if decision == "edit":
            # L'utilisateur veut autre chose : on abandonne le plan suspendu et
            # on replanifie à partir de sa correction, plutôt que de deviner.
            self.approvals.pop(message.thread_id, now)
            return await self._fresh_request(message, formatter, text, now)

        self.approvals.pop(message.thread_id, now)
        state = ExecutionState(
            results=dict(pending.results), completed=list(pending.completed)
        )
        state = await execute_plan(
            pending.plan, self.tools, state, approved_task_ids=frozenset({pending.task.id})
        )
        return await self._finish(message, formatter, text, state, now)

    async def _fresh_request(
        self,
        message: IncomingMessage,
        formatter: ChannelFormatter,
        text: str,
        now: datetime,
    ) -> str:
        context = {"channel": message.channel, "user_id": message.user_id, "now": now}

        decision = await self.router.route(text, context)
        if decision.get("complexity") == "trivial" and not decision.get("requires_tools"):
            answer = await self.responder.summarize(text, ExecutionState(), context)
            return await self._reply(message, formatter, answer)

        try:
            plan = await self.planner.plan(text, context)
        except PlanError as exc:
            return await self._reply(message, formatter, f"⚠️ Je n'ai pas pu construire le plan : {exc}")

        if not plan:
            answer = await self.responder.summarize(text, ExecutionState(), context)
            return await self._reply(message, formatter, answer)

        state = await execute_plan(plan, self.tools)
        return await self._finish(message, formatter, text, state, now, plan)

    async def _finish(
        self,
        message: IncomingMessage,
        formatter: ChannelFormatter,
        text: str,
        state: ExecutionState,
        now: datetime,
        plan: list[Task] | None = None,
    ) -> str:
        if state.suspended:
            approval = state.pending_approval
            assert approval is not None
            summary = format_approval_summary(approval.task, approval.prepared)
            self.approvals.put(
                PendingApproval(
                    thread_id=message.thread_id,
                    user_id=message.user_id,
                    task=approval.task,
                    plan=plan or [],
                    results=dict(state.results),
                    completed=list(state.completed),
                    summary=summary,
                    requested_at=now,
                )
            )
            return await self._reply(message, formatter, summary)

        context = {"channel": message.channel, "user_id": message.user_id, "now": now}
        answer = await self.responder.summarize(text, state, context)
        return await self._reply(message, formatter, answer)

    async def _reply(self, message: IncomingMessage, formatter: ChannelFormatter, body: str) -> str:
        for chunk in formatter.render(body):
            await self.sender.send_text(message.from_id, chunk)
        return body
