#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import os
import sys

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@mock_aws
def test_get_miris_integration_key_returns_string():
    """Fetch a SecretString from Secrets Manager via the wrapper."""
    region = "us-west-2"
    client = boto3.client("secretsmanager", region_name=region)
    secret_id = client.create_secret(Name="miris/integration-key", SecretString="abc123")[
        "ARN"
    ]

    from utils.secrets import get_miris_integration_key

    result = get_miris_integration_key(secret_id, region_name=region)
    assert result == "abc123"


@mock_aws
def test_get_miris_integration_key_missing_raises():
    """A non-existent secret raises (caller decides how to handle)."""
    from utils.secrets import get_miris_integration_key

    with pytest.raises(Exception):
        get_miris_integration_key(
            "arn:aws:secretsmanager:us-west-2:000000000000:secret:nope-aaaaaa",
            region_name="us-west-2",
        )
