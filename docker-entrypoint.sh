#!/bin/sh
# Container entrypoint: build the SDK package, clone the tenant-config repos
# when config backup is on, then start the server (the CMD, e.g. gunicorn).
set -e

# --- 1. SDK package ----------------------------------------------------------
# Deterministic from the packaged bytes, so every task gets the same version.
# Fatal on failure: /gbm/health would still answer 200, so a deploy would go
# green while every SDK client failed to connect.
python manage.py preparesdk

# --- 2. Tenant-config backup -------------------------------------------------
# When it is on, aviso's fileUtils pushes every tenant config change to a git
# clone of the tenant's stack repo, so those clones are made here, one per stack:
#
#   CONFIG_REPO_STACKS      stacks to clone, e.g. "gbm1-ms,gbm2-ms"
#   <STACK>_CONFIG_REPO_URL repo for a stack (gbm1-ms -> GBM1_MS_CONFIG_REPO_URL)
#   CONFIG_REPO_BRANCH      branch the backups land on (default master)
#
# With backup on, any problem below stops the task: it should not serve traffic
# while unable to make the backups it is configured to make.

# On only for BACKUP_CONFIG=True exactly; anything else, unset included, is off.
# The same rule as aviso.settings.is_backup_on(), which the backups check.
is_backup_on() {
    [ "$BACKUP_CONFIG" = "True" ]
}

if is_backup_on; then
    if [ -z "$CONFIG_REPO_STACKS" ]; then
        echo "BACKUP_CONFIG is True but CONFIG_REPO_STACKS is unset" >&2
        exit 1
    fi
    branch="${CONFIG_REPO_BRANCH:-master}"
    for stack in $(echo "$CONFIG_REPO_STACKS" | tr ',' ' '); do
        url_var="$(echo "$stack" | tr 'a-z-' 'A-Z_')_CONFIG_REPO_URL"
        url="$(printenv "$url_var" || true)"
        if [ -z "$url" ]; then
            echo "$url_var is unset but $stack is listed in CONFIG_REPO_STACKS" >&2
            exit 1
        fi
        # Skipped when the container restarts with the clone still in place.
        if [ ! -d "$CONFIG_REPO/$stack/.git" ]; then
            echo "Cloning config repo for $stack ($branch) into $CONFIG_REPO/$stack"
            git clone --branch "$branch" "$url" "$CONFIG_REPO/$stack"
        fi
    done
elif [ -n "$BACKUP_CONFIG" ] && [ "$BACKUP_CONFIG" != "False" ]; then
    echo "WARNING: BACKUP_CONFIG is '$BACKUP_CONFIG', not True: tenant config backup is off" >&2
elif [ "$mode" = "production" ]; then
    echo "WARNING: BACKUP_CONFIG is not True: tenant config changes are not backed up" >&2
fi

# --- 3. Server -----------------------------------------------------------------
exec "$@"
