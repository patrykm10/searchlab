"""Async bulk indexing into a Solr collection.

Concurrency (worker count) and batch size are the two knobs that matter for
reproducing indexing-side pathologies: merge pressure, commit storms, and
update-handler contention.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .engines import get_engine


@dataclass
class IndexStats:
    docs: int = 0
    batches: int = 0
    errors: int = 0
    started: float = field(default_factory=time.time)

    def summary(self) -> str:
        elapsed = max(time.time() - self.started, 1e-9)
        return (
            f"indexed {self.docs} docs in {elapsed:.1f}s "
            f"({self.docs / elapsed:.0f} docs/s, {self.batches} batches, {self.errors} errors)"
        )


def _read_batches(path: Path, batch_size: int):
    """Batch documents from any supported file: JSONL passes straight
    through, CSV/TSV/JSON go through the tabular reader, which maps
    columns onto Solr's dynamic fields (see tabular.py)."""
    from .tabular import read_documents, sniff_format

    if sniff_format(Path(path)) == "jsonl":
        source = _read_jsonl(path)
    else:
        source = read_documents(Path(path))
    batch: list[dict] = []
    for doc in source:
        batch.append(doc)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _read_jsonl(path: Path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


async def _worker(
    name: str,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    make_request,
    stats: IndexStats,
) -> None:
    while True:
        batch = await queue.get()
        if batch is None:
            queue.task_done()
            return
        try:
            r = await client.request(**make_request(batch))
            body_ok = r.status_code == 200 and not r.json().get("errors")
            if body_ok:
                stats.docs += len(batch)
                stats.batches += 1
            else:
                stats.errors += 1
                print(f"[{name}] HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as e:
            stats.errors += 1
            print(f"[{name}] {type(e).__name__}: {e}")
        finally:
            queue.task_done()


async def index_file(
    base_url: str,
    collection: str,
    path: str | Path,
    threads: int = 4,
    batch_size: int = 500,
    commit_within: int = 10_000,
    final_commit: bool = True,
    engine: str = "solr",
) -> IndexStats:
    eng = get_engine(engine)
    stats = IndexStats()
    queue: asyncio.Queue = asyncio.Queue(maxsize=threads * 2)

    def make_request(batch):
        return eng.bulk_request(base_url, collection, batch, commit_within)

    async with httpx.AsyncClient(timeout=120) as client:
        workers = [
            asyncio.create_task(_worker(f"w{i}", queue, client, make_request, stats))
            for i in range(threads)
        ]
        for batch in _read_batches(Path(path), batch_size):
            await queue.put(batch)
        for _ in workers:
            await queue.put(None)
        await queue.join()
        await asyncio.gather(*workers)

        if final_commit:
            if engine == "solr":
                await client.get(f"{base_url}/{collection}/update",
                                 params={"commit": "true", "wt": "json"})
            else:
                await client.post(f"{base_url}/{collection}/_refresh")

    return stats
