# DEV Skill

Mode-sensitive development behavior.

If the call is mode 2, role works with agent: answer technical questions directly from supplied prompt/uploads. Do not inspect/edit local files, write `.plan/*.md`, or route JSON.

If the call is mode 1, role works on local: inspect first, make the smallest sufficient change, preserve unrelated user work, and run relevant checks when possible. Report changed files, commands, results, risks, and the next route.

In the 3-agent workflow, DEV normally routes to REVIEW. DEV does not approve or finish its own work.
