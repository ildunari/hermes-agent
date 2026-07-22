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


def test_prettify_model_label_sol_terra_and_opus_dash_versions():
    assert prettify_model_label("gpt-5.6-sol") == "GPT 5.6 Sol"
    assert prettify_model_label("gpt-5.6-terra") == "GPT 5.6 Terra"
    assert prettify_model_label("claude-opus-4-8") == "Claude Opus 4.8"


def test_prettify_model_label_grok_and_pro_variants():
    assert prettify_model_label("grok-4.5") == "Grok 4.5"
    assert prettify_model_label("grok-composer-2.5-fast") == "Grok Composer 2.5 Fast"
    assert prettify_model_label("gpt-5.6-sol-pro") == "GPT 5.6 Sol Pro"
    assert prettify_model_label("gpt-5.6-luna") == "GPT 5.6 Luna"


def test_prettify_model_label_hides_antigravity_gemini_effort_suffix():
    # The reasoning-effort picker owns thinking depth; the effort-tier suffix
    # baked into the Antigravity Gemini route id must not show in the name.
    assert prettify_model_label("gemini-3.1-pro-low") == "Gemini 3.1 Pro"
    assert prettify_model_label("gemini-3.6-flash-high") == "Gemini 3.6 Flash"
    # Legacy route IDs remain display-compatible for persisted selections.
    assert prettify_model_label("gemini-3.5-flash-low") == "Gemini 3.5 Flash"
    assert prettify_model_label("gemini-3.5-flash-extra-low") == "Gemini 3.5 Flash"
    # Real trailing tokens that are not effort tiers are preserved.
    assert prettify_model_label("gemini-3.1-flash-image") == "Gemini 3.1 Flash Image"
    assert prettify_model_label("gemini-pro-agent") == "Gemini Pro Agent"
