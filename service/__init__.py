"""API, chatbot and dashboard over the recorded capabilities.

Deliberately outside ``src/cua/``. The core is the discover-record-replay
engine with its guardrails, evidence and escalation; this is a wrapper that
exposes it. Keeping the wrapper at the top level, alongside ``target_app/``,
means a diff of the adaptation work shows the core untouched.
"""
