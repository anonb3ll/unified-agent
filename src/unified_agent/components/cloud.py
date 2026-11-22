"""MCP tools of the ``cloud`` component.

Cloud mirror and bridge, Horizon 2 connectors, the TypeScript worker transport,
GCloud and cloud recovery tools.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import cloud_recovery, gcloud, horizon2, mcp_gateway
from .base import Component

COMPONENT = Component(
    name="cloud",
    tools=(
        horizon2.horizon2_server_list,
        horizon2.horizon2_server_register,
        horizon2.horizon2_server_revoke,
        horizon2.horizon2_connector_authorize,
        horizon2.horizon2_package_list,
        horizon2.horizon2_package_register,
        gcloud.gcloud_inventory,
        gcloud.gcloud_ops,
        cloud_recovery.cloud_worker_recover,
        mcp_gateway.mcp_gateway,
    ),
)
