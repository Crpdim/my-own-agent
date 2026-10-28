"""Cancellation handling for owned operations that must settle before returning."""

import asyncio


async def finish_before_cancel[T](task: asyncio.Task[T]) -> T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
        except Exception as error:
            raise cancellation from error
        raise
