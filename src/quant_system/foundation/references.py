"""Declared provenance keys shared by storage and replay."""

REFERENCE_KEYS = {"lineage", "basis", "input_refs", "document_ids", "evidence_refs", "adjustments", "fx_refs", "supersedes", "correction_of"}


def referenced_ids(value, key=""):
    refs = set()
    if isinstance(value, dict):
        for name, item in value.items():
            refs.update(referenced_ids(item, name))
    elif isinstance(value, list):
        for item in value:
            refs.update(referenced_ids(item, key))
    elif isinstance(value, str) and (key.endswith("_ref") or key in REFERENCE_KEYS):
        refs.add(value)
    return refs
