''' Pipeline throughput: documents/minute at 1, 2, 4 workers. See m13.md §4.3.

    python -m bench.pipeline --docs 120 --workers 1,2,4

Seeds N documents already normalized (OCR cannot run on this host), starts the workers,
and times from "all workers warm" to "every document PUSHED". Claude is canned (zero
latency); everything else is the real pipeline. The number is therefore the CPU/DB
ceiling of classify -> embed -> extract -> associate -> route -> push, excluding both
Claude and OCR. DESIGN.md §9 target: >= 20 docs/min on one worker, ~linear to 4.
'''

import bench.env  # noqa: F401  (must be first)

import argparse
import multiprocessing as mp
import os
import time

from sqlalchemy import func, select

from app.db import SessionLocal
from app.enums import DocumentStatus, DocumentType, JobKind
from app.models import Document
from bench import canned, seed


def worker_main(barrier) -> None:
    ''' One worker process: canned Claude, warm embedder, wait for the start gun. '''
    canned.patch_claude()
    from app.processing import embed

    embed.encode(["warm up"])
    barrier.wait()
    from app.jobs import worker

    worker.run_forever()


def count(tenant, *statuses) -> int:
    with SessionLocal() as s:
        return s.scalar(
            select(func.count()).where(
                Document.tenant_id == tenant, Document.status.in_(statuses)
            )
        )


def run_level(n_docs: int, n_workers: int, timeout: float) -> dict:
    with SessionLocal() as s:
        tenant = seed.new_tenant(s, "pipeline")
        seed.add_member(s, tenant, canned.MEMBER)
        for i in range(n_docs):
            seed.add_doc(
                s, tenant, status=DocumentStatus.NORMALIZED,
                doc_type=DocumentType.UNKNOWN, filename=f"claim_{i}.pdf",
                job=JobKind.CLASSIFY,
                pages=[
                    [f"CMS-1500 Health Insurance Claim Form number {i}",
                     f"Claim number: C-{i}. Member ID: M-0001. Date of service 2026-08-03.",
                     "CPT 72148 MRI lumbar spine without contrast. Billed amount $1,250.00.",
                     "Provider Northside Imaging, NPI 1234567893, place of service 22."],
                    [f"Diagnosis codes for claim {i}: M54.50 low back pain, unspecified.",
                     "Referring provider Dr. A. Patel. Prior authorization not required."],
                ],
            )
        s.commit()

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n_workers + 1)
    procs = [ctx.Process(target=worker_main, args=(barrier,), daemon=True)
             for _ in range(n_workers)]
    try:
        for p in procs:
            p.start()
        barrier.wait(timeout=300)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            pushed = count(tenant, DocumentStatus.PUSHED)
            failed = count(tenant, DocumentStatus.FAILED, DocumentStatus.NEEDS_REVIEW)
            if pushed + failed >= n_docs:
                break
            time.sleep(0.5)
        elapsed = time.monotonic() - t0
    finally:
        for p in procs:
            p.terminate()
        seed.cleanup(tenant)
    return {"workers": n_workers, "pushed": pushed, "stuck": n_docs - pushed - failed,
            "failed": failed, "elapsed": elapsed, "per_min": pushed / elapsed * 60}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", type=int, default=120)
    ap.add_argument("--workers", default="1,2,4")
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()

    mock = canned.ensure_mock_downstream()
    print(f"{args.docs} docs per level, {os.cpu_count()} logical CPUs")
    print(f"{'workers':>8} {'pushed':>7} {'failed':>7} {'stuck':>6} {'secs':>7} "
          f"{'docs/min':>9} {'per worker':>11}")
    try:
        for w in (int(x) for x in args.workers.split(",")):
            r = run_level(args.docs, w, args.timeout)
            print(f"{r['workers']:>8} {r['pushed']:>7} {r['failed']:>7} {r['stuck']:>6} "
                  f"{r['elapsed']:>7.1f} {r['per_min']:>9.1f} {r['per_min'] / w:>11.1f}")
    finally:
        if mock:
            mock.terminate()


if __name__ == "__main__":
    main()
