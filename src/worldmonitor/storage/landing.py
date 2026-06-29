"""The landing zone — raw collected records are written here verbatim.

Connectors write every raw record to S3-compatible storage (MinIO in dev) before
mapping, so the provenance pointer (`source_record`) always resolves to the exact
bytes that produced an entity. This is the single boto3 import site; the S3 client
is typed via ``boto3-stubs``.
"""

# boto3.client is a giant Literal-overload whose un-stubbed services return
# Unknown; relax that one report at this single boto3 boundary (the s3 overload
# still resolves to a fully-typed S3Client).
# pyright: reportUnknownMemberType=false
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from worldmonitor.settings import Settings, get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

# S3/MinIO ``DeleteObjects`` accepts at most 1000 keys per call (the same cap ``ListObjectsV2``
# applies per page), so a prefix sweep must batch its deletes in <=1000-key chunks.
_S3_DELETE_BATCH = 1000


@dataclass(frozen=True, slots=True)
class LandingStore:
    """Writes/reads raw records to an S3-compatible bucket (the landing zone)."""

    client: S3Client
    bucket: str

    @classmethod
    def connect(
        cls,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> LandingStore:
        """Open an S3 client against ``endpoint`` (host:port) using path addressing."""
        scheme = "https" if secure else "http"
        client = boto3.client(
            "s3",
            endpoint_url=f"{scheme}://{endpoint}",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return cls(client=client, bucket=bucket)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LandingStore:
        """Build a landing store from the process settings."""
        cfg = settings or get_settings()
        return cls.connect(
            endpoint=cfg.minio_endpoint,
            access_key=cfg.minio_access_key,
            secret_key=cfg.minio_secret_key,
            bucket=cfg.landing_bucket,
            secure=cfg.minio_secure,
        )

    def ensure_bucket(self) -> None:
        """Create the landing bucket if it does not already exist."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        """Store ``data`` at ``key`` and return its ``s3://`` URI (the provenance pointer)."""
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        """Read back the raw bytes stored at ``key``."""
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def list_keys(self, prefix: str = "") -> list[str]:
        """List EVERY object key under ``prefix``, paging past the S3/MinIO 1000-keys-per-page cap.

        A single ``list_objects_v2`` returns at most 1000 keys (``IsTruncated`` + a
        ``NextContinuationToken`` for the rest); this walks every page via the boto3 paginator so a
        GDPR erase of a >1000-object source sees the source's ENTIRE landed footprint — not just its
        first page (Gate B-4a / ADR 0049).
        """
        paginator = self.client.get_paginator("list_objects_v2")
        return [
            key
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
            for obj in page.get("Contents", [])
            if (key := obj.get("Key"))
        ]

    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (idempotent — deleting a missing key is a no-op in S3).

        Part of the GDPR right-to-erasure surface (Gate B-4a / ADR 0049): removing a source's raw
        PII bytes from the landing zone. S3 ``DeleteObject`` succeeds whether or not the key exists,
        so a repeat erase of an already-deleted object is a clean no-op.
        """
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_objects_with_metadata(self, prefix: str = "") -> list[dict[str, Any]]:
        """List ALL objects under ``prefix`` with size and last-modified metadata, paging past
        the 1000-key S3/MinIO cap.

        Each element is a ``dict`` with:
        * ``"Key"`` — the object key (``str``).
        * ``"Size"`` — object size in bytes (``int``; 0 if absent from the response).
        * ``"LastModified"`` — UTC-aware ``datetime`` set by the server at PUT time, or ``None``
          if absent from the response (treated as RECENT by the GC to be conservative).

        Used by the landing-zone orphan GC (ADR 0083 / audit M-6) to identify unreferenced
        orphaned objects without a separate per-key ``HEAD`` request.
        """
        paginator = self.client.get_paginator("list_objects_v2")
        result: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                result.append(
                    {
                        "Key": obj.get("Key", ""),
                        "Size": obj.get("Size", 0),
                        "LastModified": obj.get("LastModified"),
                    }
                )
        return result

    def delete_keys(self, keys: list[str]) -> int:
        """Delete a specific list of keys in <=1000-key batches. Fail-loud on partial errors.

        Mirrors the batch+fail-loud discipline of ``delete_prefix`` (ADR 0049, Gate B-4a):
        a non-empty ``Errors`` array in any ``DeleteObjects`` response raises ``RuntimeError``
        immediately rather than silently under-reporting. Returns the total count of confirmed
        deletions (from the ``Deleted`` array in the S3 response).

        Unlike ``delete_prefix`` (which sweeps all keys under a prefix), this method deletes
        a SPECIFIC set of keys computed by the caller (the landing-zone orphan GC, ADR 0083).
        An empty ``keys`` list is a no-op that returns 0.
        """
        deleted = 0
        for start in range(0, len(keys), _S3_DELETE_BATCH):
            batch = keys[start : start + _S3_DELETE_BATCH]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )
            errors = response.get("Errors")
            if errors:
                raise RuntimeError(
                    f"delete_keys: {len(errors)} object(s) failed to delete "
                    f"(e.g. {errors[0]}); operation INCOMPLETE — retry (idempotent)"
                )
            deleted += len(response.get("Deleted", []))
        return deleted

    def delete_prefix(self, prefix: str) -> int:
        """Delete EVERY object under ``prefix`` and return the TRUE count deleted (idempotent).

        Lists ALL keys under ``prefix`` (paged past the 1000-key list cap), then deletes them in
        <=1000-key batches because S3 ``DeleteObjects`` also caps at 1000 keys/call. Keyed on a
        source's ``"{connector_id}/{safe_dataset}/"`` prefix (Gate B-4a / ADR 0049), this erases the
        source's queue-referenced, dead-letter-referenced, AND orphaned landed bytes in one sweep —
        more complete than a per-queue-row delete. The ``/``-terminated prefix is collision-safe:
        erasing ``"ofac/sdn/"`` never sweeps ``"ofac-eu/sdn/"``. Missing keys are a no-op, so a
        repeat erase returns 0; the count returned is what ``erase_source`` audits as
        ``landing_objects_deleted``.

        A partial ``DeleteObjects`` failure (a non-empty ``Errors`` array — e.g. object lock /
        retention / a transient error) RAISES rather than silently under-reporting: on a GDPR
        right-to-erasure path a left-behind object must surface (and ``erase_source`` is idempotent,
        so the operator retries), never report a misleading "request honored" count.
        """
        keys = self.list_keys(prefix=prefix)
        deleted = 0
        for start in range(0, len(keys), _S3_DELETE_BATCH):
            batch = keys[start : start + _S3_DELETE_BATCH]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch]},
            )
            errors = response.get("Errors")
            if errors:
                raise RuntimeError(
                    f"delete_prefix({prefix!r}): {len(errors)} object(s) failed to delete "
                    f"(e.g. {errors[0]}); landing erase INCOMPLETE - retry (idempotent)"
                )
            deleted += len(response.get("Deleted", []))
        return deleted
