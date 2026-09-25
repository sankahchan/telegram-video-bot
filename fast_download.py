"""
Parallel (multi-connection) Telegram file downloader for Pyrogram.

Pyrogram's download_media() fetches 1MB chunks SEQUENTIALLY over a single
connection. On high-latency links (e.g. Asia -> Telegram's EU data centers)
throughput gets capped at roughly chunk_size / round_trip_time.

This module splits the file into chunk ranges and downloads them CONCURRENTLY
via multiple get_file() media sessions, then assembles the parts.

Requires the Client to be created with:
    max_concurrent_transmissions >= workers
"""
import asyncio
import os
import shutil

from pyrogram.file_id import FileId

CHUNK_SIZE = 1024 * 1024  # must match Pyrogram's internal download chunk size


def _get_media(msg):
    return (
        msg.photo or msg.video or msg.video_note or msg.document
        or msg.animation or msg.audio or msg.voice
    )


async def fast_download(client, msg, dest_path, workers=8, progress=None):
    """Download message media using `workers` parallel connections.

    Falls back to standard download when file size is unknown.
    progress: sync callable (current_bytes, total_bytes).
    Returns dest_path on success.
    """
    media = _get_media(msg)
    if media is None:
        raise ValueError("Message has no downloadable media")

    file_id = FileId.decode(media.file_id)
    file_size = getattr(media, "file_size", 0) or 0

    if file_size <= 0:
        return await client.download_media(msg, file_name=dest_path, progress=progress)

    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    workers = max(1, min(workers, total_chunks))

    # Split chunk ranges as evenly as possible across workers
    base, extra = divmod(total_chunks, workers)
    ranges, start = [], 0
    for i in range(workers):
        n = base + (1 if i < extra else 0)
        if n:
            ranges.append((start, start + n))
            start += n

    part_paths = [f"{dest_path}.part{i}" for i in range(len(ranges))]
    done = [0] * len(ranges)
    lock = asyncio.Lock()

    async def fetch(idx, cstart, cend, part_path):
        count = cend - cstart  # chunks
        with open(part_path, "wb") as f:
            # get_file(file_id, file_size, limit=chunks, offset=start_chunk)
            async for data in client.get_file(file_id, file_size, limit=count, offset=cstart):
                f.write(data)
                async with lock:
                    done[idx] += len(data)
                    if progress:
                        progress(min(sum(done), file_size), file_size)

    try:
        await asyncio.gather(
            *(fetch(i, s, e, p) for i, ((s, e), p) in enumerate(zip(ranges, part_paths)))
        )
    except BaseException:
        for p in part_paths:
            if os.path.exists(p):
                os.remove(p)
        raise

    with open(dest_path, "wb") as out:
        for p in part_paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, length=CHUNK_SIZE)
            os.remove(p)

    return dest_path
