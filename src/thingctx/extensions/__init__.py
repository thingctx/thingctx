# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""WoT vocabulary extensions. A TD opts into a vocabulary via JSON-LD
@context and @type annotations; import the matching module to use it. A
TD that doesn't opt in is unaffected. See extensions/README.md.
"""

VOCAB_URI = "https://thingctx.com/vocab#"

__all__ = ["VOCAB_URI"]
