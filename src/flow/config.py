import os

# Docker Labels
BRANCH_LABEL = "flow.branch"
MANAGED_LABEL = "flow.managed"

# Prefix for Docker resources
PREFIX = "flow-"

# Default Odoo configurations
DEFAULT_ODOO_VERSION = "15.0"
DEFAULT_ODOO_IMAGE = "odoo"
DEFAULT_POSTGRES_IMAGE = "postgres:15"

# Environment variables
ODOO_DB_USER = os.getenv("ODOO_DB_USER", "odoo")
ODOO_DB_PASSWORD = os.getenv("ODOO_DB_PASSWORD", "odoo")

# External Access
EXTERNAL_HOST = os.getenv("EXTERNAL_HOST", "localhost")
PORT_RANGE_START = int(os.getenv("PORT_RANGE_START", "50000"))
PORT_RANGE_END = int(os.getenv("PORT_RANGE_END", "50100"))

# Workspaces
WORKSPACES_DIR = os.getenv("FLOW_WORKSPACES_DIR", os.path.expanduser("~/.flow/workspaces"))
