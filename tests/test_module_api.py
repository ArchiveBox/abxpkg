import abxpkg
from abxpkg import EnvProvider
from abxpkg.__init__ import _PROVIDER_NAME_PRIORITY, _provider_class


class TestModuleApi:
    def test_public_exports_are_available_directly_and_by_wildcard_import(self):
        public_names = abxpkg.__all__
        namespace = {}
        exec("from abxpkg import *", namespace)

        assert isinstance(public_names, list)
        assert "EnvProvider" in public_names
        assert namespace["EnvProvider"] is EnvProvider

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

    def test_default_provider_priority_prefers_nonroot_before_root_and_source(self):
        assert _PROVIDER_NAME_PRIORITY.index("env") == 0
        apt_index = _PROVIDER_NAME_PRIORITY.index("apt")

        for provider_name in ("brew", "uv", "pip", "node", "npm", "bash", "nix"):
            assert _PROVIDER_NAME_PRIORITY.index(provider_name) < apt_index

        for provider_name in ("gem", "goget", "cargo"):
            assert apt_index < _PROVIDER_NAME_PRIORITY.index(provider_name)
