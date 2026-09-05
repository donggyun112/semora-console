"""Project native Pydantic AI control boundaries onto the console frame protocol."""

import copy

from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart
from pydantic_core import to_jsonable_python
from semora import AgentRuntime, ControlPlane, Ctx, Deny, Halt, Proceed, Suspend
from semora.dispatch import Answer


class Events:
    def __init__(self, emit):
        self.emit = emit

    async def publish(self, event_type, **payload):
        if self.emit is not None:
            await self.emit(event_type, payload)

    async def session_start(self, source):
        await self.publish("session_start", source=source)

    async def session_end(self, reason):
        await self.publish("session_end", reason=reason)


class ObservedControls:
    """Observe seven real policy seams; retain native message snapshots for branching."""

    def __init__(
        self, runtime, run_id, controls, on_event, *, aborted=None, prompt_id=None
    ):
        self.runtime = runtime
        self.run_id = run_id
        self.inner = controls or ControlPlane()
        self.on_event = on_event
        self.aborted = aborted
        self.prompt_id = prompt_id
        self.results = {}
        self.announced = set()
        self.answered = set()
        self.replayed = set()
        self.control_parts = set()

    async def emit(self, event_type, **payload):
        await self.runtime.events.publish(event_type, **payload)

    async def event(self, **event):
        if event.get("type") == "tool_result":
            self.answered.add(event["id"])
        if self.on_event is not None:
            await self.on_event(event)

    async def project_missing(self, outcome):
        """Project actual native returns when Pydantic AI skipped an execution hook.

        A human denial and a replay whose journal marker already exists both bypass
        post_tool_use. Their ToolReturnPart is still an answer that the UI must show.
        """
        history = outcome.all_messages()
        calls = {
            part.tool_call_id: part
            for message in history
            for part in message.parts
            if isinstance(part, ToolCallPart)
        }
        new = outcome.result.new_messages() if outcome.result else []
        for message in new:
            for part in message.parts:
                if (
                    not isinstance(part, ToolReturnPart)
                    or part.tool_call_id in self.answered
                ):
                    continue
                call = calls.get(part.tool_call_id)
                if call is None:
                    continue
                ctx = Ctx(turn=0, messages=history)
                await self._announce(ctx, call)
                record = await self.runtime.store.read(
                    self.run_id, f"tool:{part.tool_call_id}"
                )
                executed = record.status == "done"
                result = copy.deepcopy(part.content)
                if executed and isinstance(result, dict):
                    result["execution"] = {
                        **result.get("execution", {}),
                        "replayed": True,
                    }
                elif not executed and not isinstance(result, dict):
                    result = {"type": "error", "message": str(result)}
                await self.event(
                    type="tool_result",
                    id=part.tool_call_id,
                    name=part.tool_name,
                    executed=executed,
                    result=result,
                )
                await self.emit(
                    "context_injected",
                    kind="tool_result",
                    origin_id=part.tool_call_id,
                    message=to_jsonable_python(part),
                )

    def checkpoint(self, ctx, **extra):
        history = copy.deepcopy(ctx.messages)
        returned = {
            p.tool_call_id
            for m in history
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        }
        additions = [
            ToolReturnPart(name, value, call_id)
            for call_id, (name, value) in self.results.items()
            if call_id not in returned
        ]
        if additions:
            history.append(ModelRequest(additions))
        return {
            "history": to_jsonable_python(history),
            "from_run_id": self.run_id,
            **extra,
        }

    async def on_inputs(self, ctx, inputs):
        screened = await self.inner.on_inputs(ctx, inputs)
        if isinstance(screened, Halt):
            return screened
        for original, item in zip(inputs, screened):
            controlled = id(original.part) in self.control_parts
            self.control_parts.discard(id(original.part))
            kind = (
                "control"
                if controlled
                else "user_prompt"
                if item.kind == "user"
                else item.kind
            )
            checkpoint = {}
            if kind == "user_prompt":
                checkpoint["_checkpoint"] = self.checkpoint(
                    ctx,
                    boundary="input",
                    prompt=original.part.content,
                    origin_id=item.origin_id or self.prompt_id,
                )
            await self.emit(
                "context_injected",
                kind=kind,
                origin_id=item.origin_id or self.prompt_id,
                message=to_jsonable_python(item.part),
                **checkpoint,
            )
        return screened

    async def before_model(self, ctx):
        if self.aborted and self.aborted():
            return Halt("aborted")
        if ctx.turn >= 8:
            return Halt("policy")
        await self.emit("before_model", turn=ctx.turn)
        return await self.inner.before_model(ctx)

    async def _announce(self, ctx, call):
        if call.tool_call_id not in self.announced:
            self.announced.add(call.tool_call_id)
            await self.event(
                type="tool_call",
                id=call.tool_call_id,
                name=call.tool_name,
                input=call.args_as_dict(),
            )

    async def _decision(self, ctx, call, decision):
        if isinstance(decision, Deny):
            self.results[call.tool_call_id] = (
                call.tool_name,
                copy.deepcopy(decision.result),
            )
            await self.emit(
                "permission_denied",
                call_id=call.tool_call_id,
                name=call.tool_name,
                reason=decision.result,
            )
            await self.event(
                type="tool_result",
                id=call.tool_call_id,
                name=call.tool_name,
                executed=False,
                result=decision.result,
            )
            await self.emit(
                "context_injected",
                kind="tool_result",
                origin_id=call.tool_call_id,
                message=to_jsonable_python(
                    ToolReturnPart(call.tool_name, decision.result, call.tool_call_id)
                ),
                executed=False,
                _checkpoint=self.checkpoint(
                    ctx, boundary="result", call_id=call.tool_call_id
                ),
            )
        elif isinstance(decision, Suspend):
            await self.event(
                type="tool_result",
                id=call.tool_call_id,
                name=call.tool_name,
                executed=False,
                result=decision.request,
            )
        return decision

    async def pre_tool_use(self, ctx, call):
        if (
            await self.runtime.store.read(self.run_id, f"tool:{call.tool_call_id}")
        ).status == "done":
            self.replayed.add(call.tool_call_id)
        await self._announce(ctx, call)
        await self.emit(
            "pre_tool_use",
            call_id=call.tool_call_id,
            name=call.tool_name,
            input=call.args_as_dict(),
            _checkpoint=self.checkpoint(
                ctx, boundary="tool", call_id=call.tool_call_id
            ),
        )
        return await self._decision(ctx, call, await self.inner.pre_tool_use(ctx, call))

    async def post_tool_use(self, ctx, call, result):
        replayed = (
            call.tool_call_id in self.replayed
            or call.tool_call_id not in self.announced
        )
        await self._announce(ctx, call)
        await self.inner.post_tool_use(ctx, call, result)
        if replayed and isinstance(result, dict):
            result["execution"] = {**result.get("execution", {}), "replayed": True}
        self.results[call.tool_call_id] = (call.tool_name, copy.deepcopy(result))
        checkpoint = self.checkpoint(ctx, boundary="result", call_id=call.tool_call_id)
        await self.emit(
            "post_tool_use",
            call_id=call.tool_call_id,
            name=call.tool_name,
            input=call.args_as_dict(),
            result=result,
            _checkpoint=checkpoint,
        )
        await self.event(
            type="tool_result",
            id=call.tool_call_id,
            name=call.tool_name,
            executed=True,
            result=result,
        )
        await self.emit(
            "context_injected",
            kind="tool_result",
            origin_id=call.tool_call_id,
            message=to_jsonable_python(
                ToolReturnPart(call.tool_name, result, call.tool_call_id)
            ),
            _checkpoint=checkpoint,
        )

    async def before_finish(self, ctx, reason):
        await self.emit("before_finish", reason=reason)
        decision = await self.inner.before_finish(ctx, reason)
        if isinstance(decision, Proceed):
            for part in decision.steers:
                self.control_parts.add(id(part))
        else:
            await self.event(type="text", text=ctx.text)
        return decision

    async def on_resume(self, ctx, call, resume):
        await self._announce(ctx, call)
        await self.emit("on_resume", call_id=call.tool_call_id, name=call.tool_name)
        await self.emit(
            "pre_tool_use",
            call_id=call.tool_call_id,
            name=call.tool_name,
            input=call.args_as_dict(),
            _checkpoint=self.checkpoint(
                ctx, boundary="tool", call_id=call.tool_call_id
            ),
        )
        return await self._decision(
            ctx, call, await self.inner.on_resume(ctx, call, resume)
        )

    async def on_suspend(self, ctx, call, request, snapshot, completed):
        await self.inner.on_suspend(ctx, call, request, snapshot, completed)
        await self.emit(
            "on_suspend",
            call_id=call.tool_call_id,
            name=call.tool_name,
            request=request,
        )


class ConsoleRuntime:
    """Console observation adapter; all execution and dispatch remains in AgentRuntime."""

    def __init__(self, *, store, transcript, emit=None, owner=None, lease_ttl=60):
        self.engine = AgentRuntime(store, transcript=transcript, lease_ttl=lease_ttl)
        self.store = store
        self.events = Events(emit)

    async def dispatch(
        self,
        run_id,
        agent,
        command,
        *,
        controls=None,
        on_event=None,
        aborted=None,
        **options,
    ):
        observed = ObservedControls(
            self,
            run_id,
            controls,
            on_event,
            aborted=aborted,
            prompt_id=getattr(command, "prompt_id", None),
        )
        if isinstance(command, Answer):
            options.pop("conversation_id", None)
        outcome = await self.engine.dispatch(
            run_id, agent, command, controls=observed, **options
        )
        await observed.project_missing(outcome)
        return {
            "stop_reason": outcome.stop_reason,
            "text": outcome.output,
            "output": outcome.output,
        }

    async def committed_history(self, run_id, conversation_id):
        return await self.engine.committed_history(run_id, conversation_id)

    async def submit(self, run_id, item, **options):
        return await self.engine.submit(run_id, item)
