"""The queue, the coalescing, the limiter, and the partitioning.

Against a real Postgres, because the three mechanisms that make this queue work
— `FOR UPDATE SKIP LOCKED`, `pg_try_advisory_lock`, and `ON CONFLICT ...
RETURNING` — are all database behaviour. A fake queue would test the parts that
were never in doubt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from bridge import config
from bridge.db import create_all, get_engine, schema
from bridge.work.limiter import ProviderLimiter, TokenBucket
from bridge.work.queue import JobQueue, RepoLock, worker_id
from bridge.work.worker import Worker


@pytest.fixture()
def engine():
    pytest.importorskip("pgserver")
    eng = get_engine("embedded://pytest_queue")
    create_all(eng)
    with eng.begin() as connection:
        connection.execute(text('TRUNCATE TABLE "jobs" CASCADE'))
    return eng


@pytest.fixture()
def queue(engine):
    return JobQueue(engine)


# ------------------------------------------------------------------ enqueue / claim


def test_a_claimed_job_is_invisible_to_everyone_else(queue):
    """SKIP LOCKED is the whole concurrency story: two workers polling at the
    same instant get different rows, with no coordinator."""
    queue.enqueue(schema.JOB_GC_WEEKLY, {"n": 1})
    first = queue.claim("worker-a")
    second = queue.claim("worker-b")
    assert first is not None
    assert second is None, "a second worker must not get the job the first is holding"


def test_claiming_counts_the_attempt(queue, engine):
    queue.enqueue(schema.JOB_GC_WEEKLY, {})
    job = queue.claim("w")
    assert job.attempts == 0, "the Job reports the count BEFORE this attempt"
    with engine.connect() as connection:
        stored = connection.execute(
            select(schema.jobs.c.attempts, schema.jobs.c.status)
        ).mappings().first()
    assert stored["attempts"] == 1 and stored["status"] == schema.STATUS_RUNNING


def test_a_job_scheduled_for_later_is_not_claimable_yet(queue):
    queue.enqueue(
        schema.JOB_GC_WEEKLY, {}, run_after=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert queue.claim("w") is None


def test_an_unknown_job_type_is_rejected_at_enqueue(queue):
    """A typo'd type is a job that is enqueued, never claimed, and never noticed."""
    with pytest.raises(ValueError):
        queue.enqueue("index_commmit", {})  # note the typo


def test_types_filter_what_a_worker_will_take(queue):
    queue.enqueue(schema.JOB_GC_WEEKLY, {})
    assert queue.claim("w", types=[schema.JOB_INDEX_COMMIT]) is None
    assert queue.claim("w", types=[schema.JOB_GC_WEEKLY]) is not None


# ------------------------------------------------------------------ coalescing


def test_five_rapid_pushes_collapse_into_one_job(queue):
    """The claim phase F exists to make. Five pushes, one job, pointing at the
    LAST sha — the intermediate ones are not work anybody asked for."""
    for index in range(5):
        queue.enqueue(
            schema.JOB_INDEX_COMMIT, {"sha": f"sha{index}"}, repo="demo", coalesce=True
        )
    assert queue.depth("demo")["queued"] == 1
    job = queue.claim("w")
    assert job.payload["sha"] == "sha4", "the surviving job must point at the newest sha"


def test_coalescing_is_per_repo(queue):
    for repo in ("alpha", "beta"):
        queue.enqueue(schema.JOB_INDEX_COMMIT, {"sha": "x"}, repo=repo, coalesce=True)
        queue.enqueue(schema.JOB_INDEX_COMMIT, {"sha": "y"}, repo=repo, coalesce=True)
    assert queue.depth("alpha")["queued"] == 1
    assert queue.depth("beta")["queued"] == 1


def test_a_running_job_does_not_absorb_new_pushes(queue):
    """Coalescing into a job already being worked would lose the new sha: the
    worker has read its payload and will never look again."""
    queue.enqueue(schema.JOB_INDEX_COMMIT, {"sha": "first"}, repo="demo", coalesce=True)
    queue.claim("w")
    queue.enqueue(schema.JOB_INDEX_COMMIT, {"sha": "second"}, repo="demo", coalesce=True)
    assert queue.depth("demo")["queued"] == 1, "the new push needs its own job"


def test_without_coalescing_every_push_is_its_own_job(queue):
    for index in range(3):
        queue.enqueue(schema.JOB_INDEX_COMMIT, {"sha": str(index)}, repo="demo")
    assert queue.depth("demo")["queued"] == 3


# ------------------------------------------------------------------ the repo lock


def test_only_one_worker_can_index_a_repo_at_a_time(engine):
    first, second = RepoLock(engine, "demo"), RepoLock(engine, "demo")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True, "releasing must hand the lock on"
    second.release()


def test_different_repos_do_not_block_each_other(engine):
    alpha, beta = RepoLock(engine, "alpha"), RepoLock(engine, "beta")
    assert alpha.acquire() and beta.acquire()
    alpha.release()
    beta.release()


def test_a_job_whose_repo_is_locked_goes_back_to_the_queue(engine, queue):
    """Not a failure — somebody else is already indexing that repo. The job must
    survive with its attempt count intact, for whoever finishes first."""
    queue.enqueue(schema.JOB_INDEX_COMMIT, {"sha": "x"}, repo="demo")
    held = RepoLock(engine, "demo")
    assert held.acquire()

    worker = Worker(engine, partition=0, partitions=1)
    assert worker.run_one() is True
    assert worker.stats.skipped_locked == 1
    assert worker.stats.failed == 0, "a busy repo is not an error"

    with engine.connect() as connection:
        row = connection.execute(select(schema.jobs)).mappings().first()
    assert row["status"] == schema.STATUS_QUEUED
    held.release()


# ------------------------------------------------------------------ failure handling


def test_a_retryable_failure_keeps_the_row_and_backs_off(queue, engine):
    """Attempts and errors accumulate on the job that is failing. Four failures
    of one job is a different problem from four jobs that failed once."""
    queue.enqueue(schema.JOB_GC_WEEKLY, {})
    job = queue.claim("w")
    queue.fail(job.id, "boom", retry_in=timedelta(seconds=30))
    with engine.connect() as connection:
        row = connection.execute(select(schema.jobs)).mappings().first()
    assert row["status"] == schema.STATUS_QUEUED
    assert row["attempts"] == 1
    assert "boom" in row["last_error"]
    assert queue.claim("w") is None, "backoff must actually delay the retry"


def test_a_terminal_failure_stays_failed(queue):
    queue.enqueue(schema.JOB_GC_WEEKLY, {})
    job = queue.claim("w")
    queue.fail(job.id, "unrecoverable")
    assert queue.depth()["failed"] == 1
    assert queue.claim("w") is None


def test_a_worker_that_vanished_has_its_job_reclaimed(queue, engine):
    """A crashed process drops its row lock, but the row it flipped to `running`
    stays that way — nobody's job, and nobody doing it."""
    queue.enqueue(schema.JOB_GC_WEEKLY, {})
    job = queue.claim("dead-worker")
    with engine.begin() as connection:
        connection.execute(
            update(schema.jobs).where(schema.jobs.c.id == job.id).values(
                locked_at=datetime.now(timezone.utc) - timedelta(hours=2)
            )
        )
    assert queue.reap_stale(timedelta(minutes=30)) == 1
    assert queue.claim("live-worker") is not None


def test_a_recently_claimed_job_is_not_reaped(queue):
    queue.enqueue(schema.JOB_GC_WEEKLY, {})
    queue.claim("busy-worker")
    assert queue.reap_stale(timedelta(minutes=30)) == 0


# ------------------------------------------------------------------ partitioning


def test_partitions_split_the_queue_without_overlap(engine, queue):
    for index in range(20):
        queue.enqueue(schema.JOB_GC_WEEKLY, {"n": index})
    claimed: dict[int, list[int]] = {}
    for partition in range(4):
        taken = []
        while True:
            job = queue.claim(f"w{partition}", partition=partition, partitions=4)
            if job is None:
                break
            taken.append(job.id)
        claimed[partition] = taken

    everything = [job_id for taken in claimed.values() for job_id in taken]
    assert len(everything) == 20
    assert len(set(everything)) == 20, "a job claimed twice is work done twice"
    for partition, taken in claimed.items():
        assert all(job_id % 4 == partition for job_id in taken)


def test_one_partition_takes_everything(queue):
    """The default. `--partition 0/1` must not filter anything out."""
    for index in range(5):
        queue.enqueue(schema.JOB_GC_WEEKLY, {"n": index})
    taken = 0
    while queue.claim("w", partition=0, partitions=1):
        taken += 1
    assert taken == 5


def test_worker_ids_name_the_host_pid_and_partition():
    """This string is what somebody reads when a job has been running an hour."""
    assert "#2of4" in worker_id(2, 4)


# ------------------------------------------------------------------ the limiter


def test_a_bucket_hands_out_its_burst_then_makes_you_wait():
    bucket = TokenBucket(rate_per_second=1000, capacity=3)
    assert all(bucket.acquire(timeout=0.1) for _ in range(3))


def test_a_429_slows_everyone_down_not_just_the_caller_that_saw_it():
    """Eight workers each retrying independently is how a rate limit becomes an
    outage. One worker's 429 has to throttle the shared bucket."""
    limiter = ProviderLimiter(rate_per_second=10, capacity=10)
    before = limiter.bucket("gemini").throttle
    limiter.on_response("gemini", RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert limiter.bucket("gemini").throttle < before


def test_recovery_is_gradual_not_a_snap_back():
    """Jumping to full speed after one good response is how a system oscillates
    between hammering a provider and being throttled by it."""
    limiter = ProviderLimiter(rate_per_second=10, capacity=10)
    limiter.on_response("gemini", RuntimeError("429"))
    throttled = limiter.bucket("gemini").throttle
    limiter.on_response("gemini", None)
    recovered = limiter.bucket("gemini").throttle
    assert throttled < recovered <= 1.0


def test_providers_are_throttled_independently(engine):
    limiter = ProviderLimiter()
    limiter.on_response("gemini", RuntimeError("429"))
    assert limiter.bucket("anthropic").throttle == 1.0


def test_a_non_rate_limit_error_does_not_throttle():
    """A parse failure is not a signal about the provider's capacity."""
    limiter = ProviderLimiter()
    limiter.on_response("gemini", ValueError("bad json"))
    assert limiter.bucket("gemini").throttle == 1.0


# ------------------------------------------------------------------ schedules


def test_a_cron_expression_fires_in_its_minute_and_not_otherwise():
    worker = Worker.__new__(Worker)  # no database needed for the schedule check
    due = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)
    assert Worker._cron_due(config.CRON_VERIFY_NIGHTLY, due) is True
    assert Worker._cron_due(config.CRON_VERIFY_NIGHTLY, due + timedelta(hours=1)) is False


def test_scheduled_jobs_coalesce_so_downtime_does_not_flood_the_queue(engine, queue):
    """A worker that was down for a day should enqueue one nightly verify, not
    one for every hour it missed."""
    worker = Worker(engine, partition=0, partitions=1)
    for _ in range(5):
        queue.enqueue(schema.JOB_VERIFY_NIGHTLY, {}, coalesce=True)
    assert queue.depth()["queued"] == 1
