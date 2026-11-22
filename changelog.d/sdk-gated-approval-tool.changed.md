- **The SDK live write-conformance suite requests approval for a gated tool.**
  `requestApproval` only queues a request for a tool the hub gates; an ungated
  tool is refused with 409. The approval leg now asks for `crew_intervene`
  instead of `task_update`, overridable with
  `UA_SDK_WRITE_CONFORMANCE_APPROVAL_TOOL` and
  `UA_SDK_WRITE_CONFORMANCE_APPROVAL_PAYLOAD`.
