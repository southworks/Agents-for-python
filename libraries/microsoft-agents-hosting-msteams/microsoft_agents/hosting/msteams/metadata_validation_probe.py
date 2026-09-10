# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Intentional structural source change for CI metadata validation testing."""

# INTENTIONAL CI VALIDATION TEST: Adding a source file without a targeted owner
# update or a valid sourceReview must trigger the capability freshness check.
ENABLED = True
