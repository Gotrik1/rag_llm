from unittest.mock import Mock, patch

from llm_providers import ProviderLLM


def test_cloud_provider_omits_fixed_output_budget_by_default():
    llm = ProviderLLM(provider="openai", api_key="test", base_url="https://example.invalid", model="test")
    with patch("llm_providers.MAX_OUTPUT_TOKENS", -1), patch.object(ProviderLLM, "_post", return_value={"choices": [{"message": {"content": "answer"}}]}) as post:
        assert llm._openai_compatible("question") == "answer"
        assert "max_tokens" not in post.call_args.args[2]


def test_positive_operator_limit_remains_available():
    llm = ProviderLLM(provider="openai", api_key="test", base_url="https://example.invalid", model="test")
    with patch("llm_providers.MAX_OUTPUT_TOKENS", 10000), patch.object(ProviderLLM, "_post", return_value={"choices": [{"message": {"content": "answer"}}]}) as post:
        llm._openai_compatible("question")
        assert post.call_args.args[2]["max_tokens"] == 10000
