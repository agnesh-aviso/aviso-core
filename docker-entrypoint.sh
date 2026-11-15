#!/bin/sh
# Build the downloadable SDK package before serving. Deterministic from the
# packaged bytes, so every task ends up with the same version.
#
# A failure here is fatal on purpose: /gbm/health would still answer 200, so
# the deploy would go green while every SDK client fails to connect.
set -e

python manage.py preparesdk

# Tenant config backup: aviso.utils.fileUtils runs pull/add/commit/push inside a
# git clone of the config repo. Legacy did this clone in its own entrypoint
# (dockerimages/baseimage/init.sh); it was lost in the move to Fargate, so
# backups have been failing ever since.
#
# Legacy ran one deployment per stack, so one repo was enough. This service
# serves every stack in CONFIG_REPO_STACKS (comma-separated, e.g.
# "gbm1-ms,gbm2-ms"), and each stack has its own repo. Each is cloned to
# $CONFIG_REPO/<stack>, where fileUtils picks one by the tenant's stack. The
# URL for a stack comes from <STACK>_CONFIG_REPO_URL, named the same way as
# <STACK>_PG_DB_CONNECTION_URL: gbm1-ms -> GBM1_MS_CONFIG_REPO_URL.
#
# Guarded to match the `if ISPROD or BACKUP_CONFIG:` check in fileUtils, so a
# deployment with backup disabled does not need a repo or a token at all.
#
# Fatal on failure, like preparesdk above: a task that cannot back up tenant
# config should not quietly serve traffic pretending that it can.
if [ "$mode" = "production" ] || [ "$BACKUP_CONFIG" = "True" ]; then
    if [ -z "$CONFIG_REPO_STACKS" ]; then
        echo "CONFIG_REPO_STACKS is unset but config backup is enabled" >&2
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
        if [ ! -d "$CONFIG_REPO/$stack/.git" ]; then
            # Branch named explicitly: fileUtils runs bare pull/push, so the
            # clone's checked-out branch is the one backups land on.
            echo "Cloning config repo for $stack ($branch) into $CONFIG_REPO/$stack"
            git clone --branch "$branch" "$url" "$CONFIG_REPO/$stack"
        fi
    done
fi

exec "$@"
