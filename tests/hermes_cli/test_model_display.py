from hermes_cli.model_display import prettify_model_label


def test_prettify_model_label_strips_dates_and_separators():
    assert prettify_model_label("claude-opus-4-5-20251101") == "Claude Opus 4.5"
    assert prettify_model_label("claude-sonnet-4-20250514") == "Claude Sonnet 4"
    assert prettify_model_label("claude-3-7-sonnet-20250219") == "Claude 3.7 Sonnet"


def test_prettify_model_label_keeps_vendor_casing_and_versions():
    assert prettify_model_label("claude-fable-5") == "Claude Fable 5"
    assert prettify_model_label("gpt-5.3-codex-spark") == "GPT 5.3 Codex Spark"
    assert prettify_model_label("glm-5v-turbo") == "GLM 5v Turbo"
    assert prettify_model_label("deepseek-v4-pro") == "DeepSeek V4 Pro"
