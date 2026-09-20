"""First-party conversion gateway.

Package layout mirrors docs/DESIGN.md section 12. Each subpackage owns one
responsibility and depends only on the ones below it:

    api -> models, consent, normalise, queue, observability
    upload -> models, observability
    dlq -> models, queue
"""
