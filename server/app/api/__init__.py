"""Router registration.

Registration order is significant: FastAPI matches routes in declaration order, so a
parameterised path registered ahead of a literal one would swallow it. The order below
mirrors the original single-file declaration order, and the shadow-sensitive groups
(``/api/repository/list`` vs ``/api/repository/{repo_id}``, ``/docs/archive.zip`` vs the
``/docs/{file_path:path}`` catch-all, ``/api/access-requests/pending`` vs
``/api/access-requests/{request_id}``) all live inside a single router each, so their
relative order is fixed by the module that defines them.
"""

from app.api.routes import (
    access_requests,
    activities,
    admin,
    auth,
    branches,
    commits,
    docs,
    files,
    issues,
    notifications,
    permissions,
    pull_requests,
    releases,
    repositories,
    review_queue,
    rollback,
    search,
    security,
    server_hooks,
    ssh_keys,
    statuses,
    system,
    tags,
    tokens,
    users,
    webhooks,
)

ROUTE_MODULES = (
    auth,
    notifications,
    admin,
    system,
    repositories,
    commits,
    files,
    rollback,
    branches,
    tags,
    releases,
    pull_requests,
    issues,
    access_requests,
    users,
    permissions,
    review_queue,
    activities,
    docs,
    tokens,
    webhooks,
    statuses,
    server_hooks,
    ssh_keys,
    search,
    security,
)


def register_routers(app):
    """Attach every route module's router to the given FastAPI application."""
    for module in ROUTE_MODULES:
        app.include_router(module.router)
