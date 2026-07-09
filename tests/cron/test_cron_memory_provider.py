from cron.scheduler import _cron_job_needs_memory_provider


def test_memory_toolset_enables_external_provider_for_cron():
    assert _cron_job_needs_memory_provider({"enabled_toolsets": ["memory"]}) is True


def test_mem0_harvest_name_or_skill_enables_external_provider_for_cron():
    assert _cron_job_needs_memory_provider({"name": "nightly mem0 harvest"}) is True
    assert _cron_job_needs_memory_provider({"skills": ["hermes__mem0-first-memory-harvest_KM"]}) is True


def test_unrelated_cron_keeps_memory_provider_disabled():
    assert _cron_job_needs_memory_provider({"name": "daily weather", "enabled_toolsets": ["web"]}) is False
