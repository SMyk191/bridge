"""Webhook receivers: a push becomes a job, a deploy becomes an environment pointer.

Two endpoints, mounted on the existing FastAPI app.

`POST /webhooks/github`
    HMAC-verified, then one coalesced `index_commit`. The signature check is not
    optional and not "verify if a secret is configured": an unauthenticated
    webhook is an endpoint anyone on the internet can use to make this service do
    unbounded work. With no secret configured the endpoint **refuses every
    request** rather than accepting all of them, which is the safe direction for
    a misconfiguration to fail in.

`POST /webhooks/deploy`
    `{env, repo, sha}` -> a row in `environments`. This is what makes an
    intermediate sha worth having: commits are coalesced away by default, but a
    sha an environment points at is one somebody will ask questions about
    ("what's in staging?"), so pointing an environment at it enqueues its index.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config
from ..db import schema
from ..db.repo import IndexRepo
from .queue import JobQueue

router = APIRouter(tags=["webhooks"])


class DeployRequest(BaseModel):
    env: str = Field(min_length=1, max_length=64)
    repo: str = Field(min_length=1, max_length=256)
    sha: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")


def github_secret() -> str | None:
    return os.environ.get(config.GITHUB_WEBHOOK_SECRET_ENV) or config.provider_key_like(
        config.GITHUB_WEBHOOK_SECRET_ENV
    )


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub's `X-Hub-Signature-256`, compared in constant time.

    `compare_digest` rather than `==` because a byte-by-byte comparison leaks how
    much of a forged signature was correct, and a webhook endpoint is exactly the
    kind of thing somebody has unlimited attempts at.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])


def _repo_name(payload: dict) -> str:
    repository = payload.get("repository") or {}
    # `name` rather than `full_name`: it is what the working tree directory is
    # called, which is what everything downstream resolves against.
    return str(repository.get("name") or "")


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    secret = github_secret()
    if not secret:
        # Refusing is the safe failure. Accepting unsigned pushes because nobody
        # set a secret is how an "internal" endpoint becomes a work amplifier.
        raise HTTPException(
            status_code=503,
            detail=f"{config.GITHUB_WEBHOOK_SECRET_ENV} is not set; refusing unverified webhooks",
        )

    body = await request.body()
    if not verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad or missing signature")

    payload = await request.json()
    if x_github_event == "ping":
        return {"ok": True, "pong": True}
    if x_github_event and x_github_event != "push":
        return {"ok": True, "ignored": x_github_event}

    repo_name = _repo_name(payload)
    sha = payload.get("after") or payload.get("head_commit", {}).get("id")
    if not repo_name or not sha:
        raise HTTPException(status_code=422, detail="push payload named no repository or sha")

    queue = JobQueue(request.app.state.db_engine)
    job_id = queue.enqueue(
        schema.JOB_INDEX_COMMIT,
        {"sha": sha, "ref": payload.get("ref"), "source": "github"},
        repo=repo_name,
        coalesce=True,
    )
    return {"ok": True, "repo": repo_name, "sha": sha, "job_id": job_id, "coalesced": True}


@router.post("/webhooks/deploy")
async def deploy_webhook(payload: DeployRequest, request: Request) -> dict[str, Any]:
    """Point an environment at a sha, and make sure that sha is indexed.

    The one case where an intermediate commit earns a snapshot of its own:
    coalescing throws away shas nobody asked about, but "what is in staging?" is
    a question about a specific commit, so pointing an environment at one is the
    signal that it now matters.
    """
    engine = request.app.state.db_engine
    IndexRepo(engine).set_environment(payload.env, payload.repo, payload.sha)
    job_id = JobQueue(engine).enqueue(
        schema.JOB_INDEX_COMMIT,
        {"sha": payload.sha, "source": "deploy", "env": payload.env},
        repo=payload.repo,
        coalesce=True,
    )
    return {"ok": True, "env": payload.env, "repo": payload.repo, "sha": payload.sha, "job_id": job_id}


@router.get("/health/index")
async def health_index(request: Request) -> dict[str, Any]:
    from .health import index_health

    return index_health(request.app.state.db_engine)
