"""Drain accepted audio and its transcript owner before normal listen teardown."""

import asyncio

from starlette.websockets import WebSocketState

from .operator_ai import current


def runtime(original):
    class MiMoListenRuntime(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            supervise = self.task_supervisor.supervise

            async def with_audio_ownership(*, receive_task):
                return await supervise_disconnect(self, supervise, receive_task)

            self.task_supervisor.supervise = with_audio_ownership

    return MiMoListenRuntime


async def supervise_disconnect(host, supervise, receive_task):
    from utils.async_tasks import SupervisorResult

    result = await supervise(receive_task=receive_task)
    if (
        result.reason not in {'disconnect', 'lifetime_done'}
        or host.use_custom_stt
        or host.request.websocket.client_state != WebSocketState.DISCONNECTED
        or host.request.owner_persistence_blocked.is_set()
        or host.state.close_code != 1000
        or host.state.stt_terminal_failure
    ):
        return result

    async def drain():
        # A heartbeat can observe peer closure while receive_data is awaiting
        # the last ASR window. That observation must not cancel accepted audio.
        await asyncio.shield(receive_task)
        consumers = [
            task for task in host.task_supervisor.monitored_tasks if task.get_name().endswith(':stream_transcript')
        ]
        if len(consumers) != 1:
            raise RuntimeError('listen transcript task owner is missing')
        await asyncio.shield(consumers[0])
        # The existing consumer can finish before late ASR callbacks arrive.
        # Re-enter that same persistence owner only after its prior task ended.
        if not host.request.owner_persistence_blocked.is_set() and (
            host.transcripts.segment_buffer or host.transcripts.photo_buffer
        ):
            await host.transcripts.process_loop()

    try:
        # At most three five-second windows fit in the socket's admitted buffer.
        await asyncio.wait_for(drain(), timeout=3 * current().request_timeout_seconds)
    except BaseException:
        host.state.stt_terminal_failure = True
        host.state.close_code = 1011
        raise
    return SupervisorResult(reason='disconnect', task_name=receive_task.get_name())
