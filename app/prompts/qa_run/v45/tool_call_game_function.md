---
version: v45
note: Unified game function calls and read-only observations.
placeholders: []
---
Call a Game Function listed in this run's catalog and allowed for the current case. Supply its name and arguments. Specify catalog kind only to disambiguate a name. Read-only functions return observations. Use their query-N IDs in verdict evidence, with kind query, object set to the ID, and value set to the exact returned text. Setup functions change state. Running functions return a behavior ID and accept max_seconds. Stop or extend a running function using its ID.
