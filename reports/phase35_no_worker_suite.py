"""Reproduce the Phase 35 suite subset from an isolated project copy."""

import inspect
from pathlib import Path
import re
import sys
import unittest


EXPECTED_EXCLUSIONS = {
    "test_sources_and_search.SourceAndSearchTests."
    + name
    for name in (
        "test_hash_queue_hashes_full_content_and_records_exact_duplicate_locations",
        "test_hash_worker_never_commits_partial_or_changed_file_hash",
        "test_hash_worker_pause_resume_and_single_active_lease",
        "test_hash_worker_retries_transient_failure_and_changed_fingerprint",
        "test_hash_worker_runs_successive_batches_and_resumes_after_restart",
        "test_hashing_retries_after_source_disconnect_and_preserves_audit_records",
        "test_source_root_relocation_rebases_queue_paths_atomically",
    )
}


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


root = Path(__file__).resolve().parent.parent
if Path.cwd().resolve() != root:
    raise RuntimeError("Run from the root of the isolated project copy.")
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover("tests")
excluded = []
for case in cases(suite):
    cls = type(case)
    name = case._testMethodName
    method = getattr(cls, name)
    if re.search(r"\b(?:run_hash_worker|run_hashing)\s*\(", inspect.getsource(method)):
        excluded.append(case.id())
        setattr(cls, name, unittest.skip("Worker start prohibited for Phase 35")(method))
if set(excluded) != EXPECTED_EXCLUSIONS:
    raise RuntimeError("Worker-test inventory changed; inspect it before running this suite.")

result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
