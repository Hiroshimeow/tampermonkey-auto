# AUDIT Skill

Mode-sensitive audit behavior.

If the call is mode 2, role works with agent: compare supplied evidence/uploads and answer directly. Do not inspect local repo, write `.plan/*.md`, or route JSON.

If the call is mode 1, role works on local: verify claims against files, commands, outputs, logs, and artifacts. Separate proven facts from unverified claims. Report remaining uncertainty explicitly.
