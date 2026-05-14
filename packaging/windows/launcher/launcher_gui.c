/*
 * launcher_gui.c
 *
 * Subsystem WINDOWS launcher for the Report Anonymizer GUI. Spawns the
 * bundled Python embeddable with `-m gui.main`, propagates the exit code,
 * and never shows a console window.
 *
 * See launcher_common.h for the shared environment-setup logic and edge
 * case mitigations.
 */
#include "launcher_common.h"

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                    LPWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;

    /* F4: mitigate DLL hijacking. Must be the FIRST call before any other
     * DLL is implicitly loaded. */
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);

    LPWSTR launcher_dir = launcher_get_exe_dir();
    if (!launcher_dir) {
        MessageBoxW(NULL, L"Could not resolve launcher path.",
                    L"Report Anonymizer", MB_ICONERROR | MB_OK);
        return 1;
    }
    LPWSTR app_root = launcher_get_app_root(launcher_dir);
    if (!app_root) {
        LocalFree(launcher_dir);
        return 1;
    }

    launcher_setup_env(app_root);

    /* IMPORTANT: spawn pythonw.exe (GUI subsystem) and NOT python.exe
     * (console subsystem). This launcher is itself GUI, but a GUI process
     * spawning a console child causes Windows to allocate a fresh console
     * window for the child -- which is exactly the "black terminal stays
     * open" symptom users report. pythonw.exe is shipped in the Python
     * embeddable distribution alongside python.exe and is the canonical
     * Windows entry point for windowed Python programs. */
    LPWSTR python_exe = launcher_concat_w(2, app_root, L"\\python\\pythonw.exe");
    LPWSTR repo_dir   = launcher_concat_w(2, app_root, L"\\repo");

    /* Forward any extra argv the user might have passed via "Open with...". */
    int wargc = 0;
    LPWSTR *wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);

    int fwd_argc = (wargc > 1) ? (wargc - 1) : 0;
    LPCWSTR *fwd_argv = NULL;
    if (fwd_argc > 0) {
        fwd_argv = (LPCWSTR *)LocalAlloc(LPTR, (size_t)fwd_argc * sizeof(LPCWSTR));
        for (int i = 0; i < fwd_argc; ++i) fwd_argv[i] = wargv[i + 1];
    }

    LPCWSTR target_args[] = { L"-m", L"gui.main" };
    int rc = launcher_run_child(python_exe, target_args, 2,
                                fwd_argc, fwd_argv, repo_dir);

    if (fwd_argv)   LocalFree(fwd_argv);
    if (wargv)      LocalFree(wargv);
    if (python_exe) LocalFree(python_exe);
    if (repo_dir)   LocalFree(repo_dir);
    if (app_root)   LocalFree(app_root);
    if (launcher_dir) LocalFree(launcher_dir);
    return rc;
}
