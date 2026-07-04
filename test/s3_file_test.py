from __future__ import annotations

import logging

import boto3
import pytest
from moto.server import ThreadedMotoServer

import avalanche as ava


@pytest.fixture
def moto_s3_endpoint():
    logger = logging.getLogger("werkzeug")
    previous_level = logger.level
    logger.setLevel(logging.ERROR)

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    try:
        host, port = server.get_host_and_port()
        yield f"http://{host}:{port}"
    finally:
        server.stop()
        logger.setLevel(previous_level)


def test_s3_file_reads_from_moto_with_authenticated_s3fs(moto_s3_endpoint):
    access_key = "test-access-key"
    secret_key = "test-secret-key"
    bucket = "avalanche-s3file-test"
    key = "inputs/document.txt"
    body = b"moto-authenticated-s3-content"

    client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url=moto_s3_endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    client.create_bucket(Bucket=bucket)
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")

    remote = ava.S3File(uri=f"s3://{bucket}/{key}", content_type="text/plain")
    endpoint_options = {
        "client_kwargs": {
            "endpoint_url": moto_s3_endpoint,
            "region_name": "us-east-1",
        }
    }

    with pytest.raises(PermissionError, match="Forbidden"):
        remote.read_bytes(anon=True, **endpoint_options)

    auth_options = {"key": access_key, "secret": secret_key, **endpoint_options}
    assert remote.read_bytes(**auth_options) == body
    with remote.open(**auth_options) as file:
        assert file.read() == body
