#!/usr/bin/env bash
set -Eeuo pipefail

test_name="$(basename "${1:?test path is required}" .py)"

case "${test_name}" in
    test_ansibleprovider)
        printf '%s\n' manager_binaries python_cli_binaries
        ;;
    test_aptprovider|test_binary_service_apt)
        printf '%s\n' linux_binaries
        ;;
    test_bashprovider|test_binary|test_install|test_npmprovider)
        printf '%s\n' node_npm_binaries
        ;;
    test_brewprovider)
        printf '%s\n' manager_binaries
        ;;
    test_bunprovider)
        printf '%s\n' manager_binaries bun_binaries
        ;;
    test_cargoprovider)
        printf '%s\n' manager_binaries cargo_binaries
        ;;
    test_chromewebstoreprovider)
        printf '%s\n' node_npm_binaries host_utility_binaries
        ;;
    test_denoprovider)
        printf '%s\n' manager_binaries deno_binaries
        ;;
    test_envprovider)
        printf '%s\n' manager_binaries host_utility_binaries
        ;;
    test_gemprovider)
        printf '%s\n' manager_binaries gem_binaries
        ;;
    test_gogetprovider)
        printf '%s\n' manager_binaries go_binaries
        ;;
    test_nixprovider)
        printf '%s\n' manager_binaries
        ;;
    test_playwrightprovider|test_pnpmprovider|test_puppeteerprovider)
        printf '%s\n' node_npm_binaries pnpm_binaries
        ;;
    test_pyinfraprovider)
        printf '%s\n' manager_binaries python_cli_binaries
        ;;
    test_yarnprovider)
        printf '%s\n' node_npm_binaries yarn_binaries
        ;;
    test_cli|test_security_controls)
        printf '%s\n' manager_binaries node_npm_binaries pnpm_binaries
        ;;
    test_binprovider)
        printf '%s\n' manager_binaries node_npm_binaries pnpm_binaries yarn_binaries bun_binaries deno_binaries python_cli_binaries
        ;;
    test_installer_binary_contracts)
        printf '%s\n' manager_binaries node_npm_binaries pnpm_binaries go_binaries cargo_binaries gem_binaries python_cli_binaries
        ;;
    test_central_lib_dir)
        printf '%s\n' manager_binaries node_npm_binaries pnpm_binaries yarn_binaries bun_binaries deno_binaries go_binaries cargo_binaries gem_binaries python_cli_binaries host_utility_binaries
        ;;
esac

if [[ "${2:-}" == "Linux" ]]; then
    case "${test_name}" in
        test_ansibleprovider|test_pyinfraprovider)
            printf '%s\n' linux_binaries
            ;;
    esac
fi
