"""Execution guards retain provenance warnings on explicit synthetic inputs."""


SYNTHETIC_PROVENANCE = frozenset({"SOURCE_TIME_UNKNOWN", "SOURCE_SEQUENCE_UNAVAILABLE", "SOURCE_EVENT_ID_UNAVAILABLE"})


def execution_book_issues(message):
    flags = set(message["quality_flags"])
    # These describe unavailable exchange metadata, not an unknown local
    # simulation clock. Every other flag, including unknown flags, blocks.
    return flags - SYNTHETIC_PROVENANCE if message["data_origin"] == "SYNTHETIC" else flags
