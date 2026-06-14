from abxpkg import EnvProvider
from abxpkg.__init__ import _provider_class


class TestModuleApi:
    def test_provider_class_normalization_accepts_classes_and_instances(self):
        normalized_from_class = _provider_class(EnvProvider)
        normalized_from_instance = _provider_class(
            EnvProvider(postinstall_scripts=True, min_release_age=3),
        )

        assert normalized_from_class is EnvProvider
        assert normalized_from_instance is EnvProvider
        assert normalized_from_instance.model_fields["name"].default == "env"
        assert normalized_from_instance.__name__ == "EnvProvider"

    def test_mixed_provider_entries_produce_valid_names_class_names_and_classes(self):
        providers = [
            EnvProvider,
            EnvProvider(postinstall_scripts=True, min_release_age=3),
        ]

        provider_names = [
            _provider_class(provider).model_fields["name"].default
            for provider in providers
        ]
        provider_class_names = [
            _provider_class(provider).__name__ for provider in providers
        ]
        provider_class_by_name = {
            _provider_class(provider).model_fields["name"].default: _provider_class(
                provider,
            )
            for provider in providers
        }

        assert provider_names == ["env", "env"]
        assert provider_class_names == ["EnvProvider", "EnvProvider"]
        assert provider_class_by_name["env"] is EnvProvider
