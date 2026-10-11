# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Observation, Actuation and Refusal, in SOSA terms.

SOSA (the W3C and OGC sensor ontology) defines observations and actuations. It has
no term for who asked, how a command ended, where the raw data is, or the reason a
gate gave, so those fields use PROV-O, schema.org, DCAT and Dublin Core.

``records.yaml`` defines the model. The generator writes everything in ``_generated``
from it.
"""

from __future__ import annotations

from thingctx.models.sosa._generated.records import Actuation, Observation, Refusal
from thingctx.models.sosa._generated.terms import CONTEXT
from thingctx.models.sosa._generated.terms import OUTCOMES as _ENUMS

__all__ = ["CONTEXT", "OUTCOMES", "Actuation", "Observation", "Refusal"]

# The ways an accepted ROS goal can end, from action_msgs/msg/GoalStatus.
OUTCOMES = frozenset(_ENUMS["ActuationOutcome"])
