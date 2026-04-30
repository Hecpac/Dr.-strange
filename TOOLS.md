# TOOLS.md - Local Tool Notes

This file records local tool conventions for the agent.

## Defaults
- Prefer semantic runtime tools before shell escape hatches.
- Use absolute paths for file operations.
- Verify command results before reporting success.

## Tool Execution Guidelines
- Evidence required: never report success for a state-changing operation, such as a deploy, code edit, or service restart, until a read/inspection tool verifies the new state. Success is demonstrated by environment confirmation, not by command execution alone.
- Action rationale: when executing a tool, provide a clear and concise intent, no more than 240 characters, stating exactly what you will inspect or modify and why.
- Failure handling: if a tool returns an error or unexpected result, acknowledge the failure immediately. Do not mask the error or assume a positive result. Use the error evidence to adjust the next step.
