
\"\"\"Controls the flow of sensitive data from local memory to external providers.\"\"\"

from __future__ import annotations

from enum import IntEnum
from dataclasses import dataclass
from typing import Any

class PrivacyLevel(IntEnum):
    PUBLIC = 0    # Can be sent anywhere
    PRIVATE = 1   # Only to authorized providers (verified local-first)
    SECRET = 2    # Never leaves the local machine

@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allow_cloud: bool
    allow_private_cloud: bool
    require_explicit_consent: bool

class EgressGuard:
    \"\"\"Validates if a piece of data can be sent to a specific provider.\"\"\"

    def __init__(self, policy: EgressPolicy):
        self._policy = policy

    def can_send(self, privacy_level: PrivacyLevel, provider_is_cloud: bool) -> bool:
        if privacy_level == PrivacyLevel.SECRET:
            return False
        
        if provider_is_cloud:
            if not self._policy.allow_cloud:
                return False
            if privacy_level == PrivacyLevel.PRIVATE and not self._policy.allow_private_cloud:
                return False
                
        return True

    def filter_content(self, content: str, privacy_level: PrivacyLevel, provider_is_cloud: bool) -> str:
        \"\"\"Simple redaction if the data is too sensitive for the provider.\"\"\"
        if not self.can_send(privacy_level, provider_is_cloud):
            return \"[REDACTED DUE TO PRIVACY POLICY]\"
        return content
