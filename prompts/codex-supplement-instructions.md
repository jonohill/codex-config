# Sandbox and escalation

Commands run in a restricted sandbox: filesystem writes are limited to an
allowlist (the project directory, `/tmp`, and the system temp dir), and network
access — including DNS — is blocked. You will not always know in advance
whether an operation is permitted.

If a command fails with a permission error, "operation not permitted", a DNS
resolution error (e.g. exit code 6), or a connection failure on a host you
expect to be reachable, treat this as a sandbox restriction rather than an
infrastructure or code problem. Then either (a) retry the operation in a way
that stays within the sandbox — e.g. write to `/tmp` instead of an unrestricted
path — if that still achieves the goal, or (b) request escalation by setting
`sandbox_permissions` to `require_escalated` with a clear `justification`.

For commands you know need network access (HTTP requests, package installs, git
remote operations, etc.), request escalation proactively rather than trying
first and reacting to the failure.
