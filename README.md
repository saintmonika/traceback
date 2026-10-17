# Traceback

Trace the fix.

Traceback turns a one-line goal into a checkable definition of done, repairs a bug in an isolated workspace, audits its repaired code with generated mutations, closes real verification gaps with agent-written checks, and reports an honest bounded outcome.

Built during the Recursive Agents Hackathon, Saturday October 17, 2026.

## Test input

`fixture/pricing-demo/` is a copy of the open-source sample project
https://github.com/saintmonika/pricing-demo (MIT license, published October 16, 2026, before the event).
Its `.git` folder was not copied. Traceback's code does not refer to anything specific in this project; the only reference is the fixture path in the configuration.

## Status

Work in progress. This section will list what is implemented and what is not.

## Safety note

Traceback is not a secure sandbox. It supports only the trusted fixture project.