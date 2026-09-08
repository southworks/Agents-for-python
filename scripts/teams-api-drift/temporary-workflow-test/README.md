# TEMPORARY scheduled-workflow test fixture

> **REMOVE THIS ENTIRE DIRECTORY BEFORE PREPARING THE PRODUCTION PR.**

This directory is intentionally committed only while manually exercising the
scheduled Teams API drift workflow.

The wheel is a complete copy of the official
`microsoft-teams-api==2.0.16` package, repackaged as `2.99.901` with four
deliberate changes:

- blocking: `ChannelInfo.id` changes from `Optional[str]` to
  `Optional[int]`;
- required: `NotificationInfo.alert` changes from `Optional[bool]` to
  `Optional[str]`;
- review: `ManualReviewCapability` is added in a mapped capability;
- no-action: `ManualNoActionProbe` is added outside mapped capabilities.

The scheduled workflow temporarily sets `PIP_FIND_LINKS` to this directory
and overrides the resolved comparison pair to `2.0.16 -> 2.99.901`. Search
for `BEGIN TEMPORARY WORKFLOW TEST` in the scheduled workflow to find every
YAML line that must be removed.

Expected SHA-256 for the wheel:

```text
8643DD11254593C71DE9A1ACF4A6561FDDCEF901542B9F34D055CE55D0F43D7D
```
