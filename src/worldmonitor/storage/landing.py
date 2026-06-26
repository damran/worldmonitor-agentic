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
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from worldmonitor.settings import Settings, get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


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
        """List object keys under ``prefix`` (used by tests/inspection)."""
        response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return [key for obj in response.get("Contents", []) if (key := obj.get("Key"))]

    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (idempotent — deleting a missing key is a no-op in S3).

        Part of the GDPR right-to-erasure surface (Gate B-4a / ADR 0049): removing a source's raw
        PII bytes from the landing zone. S3 ``DeleteObject`` succeeds whether or not the key exists,
        so a repeat erase of an already-deleted object is a clean no-op.
        """
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        """Delete EVERY object under ``prefix`` and return how many were deleted (idempotent).

        Lists the keys under ``prefix`` then deletes each. Keyed on a source's
        ``"{connector_id}/{safe_dataset}/"`` prefix (Gate B-4a / ADR 0049), this erases the source's
        queue-referenced, dead-letter-referenced, AND orphaned landed bytes in one sweep — more
        complete than a per-queue-row delete. The ``/``-terminated prefix is collision-safe: erasing
        ``"ofac/sdn/"`` never sweeps ``"ofac-eu/sdn/"``. Returns 0 (no-op) when the prefix is empty.
        """
        keys = self.list_keys(prefix=prefix)
        for key in keys:
            self.delete(key)
        return len(keys)
