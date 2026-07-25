## What changed and why

<!-- The diff shows what. This is for why: the decision, the constraint, the thing
you rejected. If it closes an issue, say "Closes #N". -->

## How you checked it

<!-- Which tests you ran, and what you added. A new transport wants a test that
drives it; a fix wants a test that fails without the fix. -->

## Before you open it

- [ ] `pytest -m "not network"` passes
- [ ] Every commit is signed off (`git commit -s`), or the DCO check will fail
- [ ] No secrets, tokens, or personal paths in the diff, including in test fixtures
- [ ] If you changed how a Thing Description projects into tools, `docs/MAPPING.md` moves with it
