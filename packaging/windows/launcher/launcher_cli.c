/*
 * launcher_cli.c
 *
 * Subsystem CONSOLE launcher for the Report Anonymizer CLI. Spawns the
 * bundled Python embeddable with `bin\anonymize-dossier` and the user's
 * argv. Inherits stdio so that the user sees the CLI's output directly.
 *
 * See launcher_common.h for the shared environment-setup logic.
 */
#include "launcher_common.h"

int wmain(int argc, wchar_t **argv) {
    /* F4: DLL search policy first. */
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);

    LPWSTR launcher_dir = launcher_get_exe_dir();
    if (!launcher_dir) {
        fwprintf(stderr, L"report-anonymizer-cli: could not resolve launcher path\n");
        return 1;
    }
    LPWSTR app_root = launcher_get_app_root(launcher_dir);
    if (!app_root) {
        LocalFree(launcher_dir);
        return 1;
    }

    launcher_setup_env(app_root);

    LPWSTR python_exe = launcher_concat_w(2, app_root, L"\\python\\python.exe");
    LPWSTR repo_dir   = launcher_concat_w(2, app_root, L"\\repo");
    LPWSTR cli_script = launcher_concat_w(2, app_root, L"\\repo\\bin\\anonymize-dossier");

    int fwd_argc = (argc > 1) ? (argc - 1) : 0;
    LPCWSTR *fwd_argv = NULL;
    if (fwd_argc > 0) {
        fwd_argv = (LPCWSTR *)LocalAlloc(LPTR, (size_t)fwd_argc * sizeof(LPCWSTR));
        for (int i = 0; i < fwd_argc; ++i) fwd_argv[i] = argv[i + 1];
    }

    /* The script file lacks a .py extension (matches the POSIX `bin/`
     * convention) so we invoke the Python interpreter explicitly. */
    LPCWSTR target_args[] = { cli_script };
    int rc = launcher_run_child(python_exe, target_args, 1,
                                fwd_argc, fwd_argv, repo_dir);

    if (fwd_argv)   LocalFree(fwd_argv);
    if (python_exe) LocalFree(python_exe);
    if (repo_dir)   LocalFree(repo_dir);
    if (cli_script) LocalFree(cli_script);
    if (app_root)   LocalFree(app_root);
    if (launcher_dir) LocalFree(launcher_dir);
    return rc;
}
