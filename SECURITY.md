# Security boundary and vulnerability reports

IncidentDesk is a single-user local engineering beta. The API binds to loopback and uses Host/Origin checks, while state is protected by operating-system user permissions. These measures do not provide multi-user authorization, tenant isolation, or protection from a malicious program already controlling the same system account. Do not expose this API with port forwarding, an all-interface listener, or an unprotected reverse proxy.

## Data and model access

A cloud diagnosis sends the operator's question and selected evidence to the configured provider. Local embeddings do not make LLM diagnosis offline. The explicit embedding installation downloads pinned public model files; normal embedding inference uses only the installed local model. Rule-based redaction can miss business identifiers, private content, and custom credentials. Register only authorized files and endpoints, and confirm your provider's data-handling policy before using real data.

API keys are backend-only versioned files with permissions 600 in directories with permissions 700. They are not encrypted. Old credentials remain available for frozen task versions; clearing the active connection does not erase historical versions. Databases, checkpoints, evidence, receipts, and backups may contain sensitive data and project locations. Backups preserve credentials, exclude downloaded embedding weights and process ownership, and are neither encrypted nor signed. Integrity hashes detect corruption; they do not authenticate an archive's author. Never restore an untrusted archive.

## Actions and collection

Collection is limited to registered project paths and bounded local endpoints. Avoid registering files containing secrets as editable configuration. Managed actions use operator-registered arguments and paths, frozen previews, approval digests, and execution-time checks. Approval authorizes a particular action; it cannot guarantee remediation. Treat an uncertain external action result as requiring inspection, not automatic retry. A passing health check is not evidence that all business requests work.

Do not submit credentials, real logs, private source, personal paths, complete runtime state, or backup archives to public issues, pull requests, screenshots, recordings, or CI. `.gitignore` cannot remove information already committed or published. The source distribution includes synthetic examples only; third-party application source and private integration patches require separate authorization and are not part of the public release.

## Report a vulnerability

Use [Security → Report a vulnerability](https://github.com/Ruyue213/IncidentDesk/security/advisories/new); private vulnerability reporting is enabled for this repository. If a private channel is unavailable, open an issue requesting a private contact route without publishing exploit details; wait for the maintainer's response before sharing technical information. Do not infer a maintainer email address.

Include the affected version or commit, operating system, installation method, minimal synthetic reproduction, expected/actual behavior, and impact. Remove provider keys, private paths, and third-party data. Keep details private until the maintainer agrees on a fix and disclosure plan. No fixed response SLA or long-term support branch is currently promised.

简体中文说明：当前仅支持本机单用户，不提供公网／多租户权限保障；云端诊断会发送选中的证据，本地向量模型不等于完全离线。凭证、数据库、备份仅受文件权限保护，未加密。人工审批绑定具体操作，不能保证修复有效。请勿公开真实日志、Key、私人路径或备份；漏洞优先通过仓库私密报告入口提交，没有入口时先请求私密联系方式。
