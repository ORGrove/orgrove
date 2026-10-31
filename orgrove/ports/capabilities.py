"""Capability flags: what an adapter's platform can do. Behaviour keys off these, never off adapter names."""
from enum import Enum


class Capability(str, Enum):
    NATIVE_STATES = "native_states"        # the tracker has real workflow states, not only labels
    SCOPED_LABELS = "scoped_labels"        # the platform itself keeps one stage label at a time
    PER_ROLE_TOKENS = "per_role_tokens"    # a credential can be scoped to one role
    SUB_ITEMS = "sub_items"                # create_item(parent=...) and children() work
    ASSIGNEES = "assignees"                # assign() works
    REVIEW_REQUESTS = "review_requests"    # request_review() works
    MERGED_SINCE = "merged_since"          # merged_since() can list recent merges
