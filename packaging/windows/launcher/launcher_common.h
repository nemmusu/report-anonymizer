/*
 * launcher_common.h
 *
 * Shared helpers for the Report Anonymizer Windows launchers. Both the GUI
 * launcher (ReportAnonymizer.exe) and the CLI launcher
 * (report-anonymizer-cli.exe) build the same environment block, prepend the
 * same set of paths to PATH, and spawn python.exe as a child process. The
 * only differences are:
 *   - subsystem: WINDOWS vs CONSOLE
 *   - target script: -m gui.main  vs  bin\anonymize-dossier
 *
 * Everything below uses the wide-character (W) Win32 API so that install
 * paths containing Unicode characters or spaces work out of the box.
 *
 * Edge cases covered (plan §6 F1-F7, E5):
 *   - F1 paths with spaces:  CreateProcessW + quoted command line.
 *   - F2 Unicode paths:      W APIs end-to-end; manifest activeCodePage=UTF-8.
 *   - F3 argv forwarding:    GetCommandLineW + CommandLineToArgvW + skip [0].
 *   - F4 DLL hijacking:      SetDefaultDllDirectoriesW first in wWinMain.
 *   - F6 exit code:          GetExitCodeProcess + return as int.
 *   - E5 PYTHONPATH leakage: explicit SetEnvironmentVariableW("PYTHONPATH", NULL).
 */
#ifndef REPORT_ANONYMIZER_LAUNCHER_COMMON_H
#define REPORT_ANONYMIZER_LAUNCHER_COMMON_H

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

/* SetDefaultDllDirectories flags (defined in winbase.h on modern SDKs but
 * the mingw-w64 headers we ship may have an older copy; redefine
 * defensively). */
#ifndef LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
#define LOAD_LIBRARY_SEARCH_DEFAULT_DIRS 0x00001000
#endif

/* Return the directory containing this launcher executable (no trailing
 * backslash). The caller owns the returned heap buffer and must LocalFree
 * it. */
static LPWSTR launcher_get_exe_dir(void) {
    DWORD cap = MAX_PATH;
    LPWSTR buf = (LPWSTR)LocalAlloc(LPTR, cap * sizeof(WCHAR));
    if (!buf) return NULL;
    for (;;) {
        DWORD got = GetModuleFileNameW(NULL, buf, cap);
        if (got == 0) {
            LocalFree(buf);
            return NULL;
        }
        if (got < cap) break;
        cap *= 2;
        LocalFree(buf);
        buf = (LPWSTR)LocalAlloc(LPTR, cap * sizeof(WCHAR));
        if (!buf) return NULL;
    }
    /* Strip filename. */
    LPWSTR slash = wcsrchr(buf, L'\\');
    if (slash) *slash = L'\0';
    return buf;
}

/* Compute the install root: launcher lives in <app>\launcher\<exe>, so the
 * install root is its parent directory. Returns heap buffer (LocalFree). */
static LPWSTR launcher_get_app_root(LPCWSTR launcher_dir) {
    size_t n = wcslen(launcher_dir);
    LPWSTR buf = (LPWSTR)LocalAlloc(LPTR, (n + 1) * sizeof(WCHAR));
    if (!buf) return NULL;
    wcscpy_s(buf, n + 1, launcher_dir);
    LPWSTR slash = wcsrchr(buf, L'\\');
    if (slash) *slash = L'\0';
    return buf;
}

/* Read an environment variable into a freshly-allocated buffer (LocalFree). */
static LPWSTR launcher_getenv_w(LPCWSTR name) {
    DWORD need = GetEnvironmentVariableW(name, NULL, 0);
    if (need == 0) return NULL;
    LPWSTR buf = (LPWSTR)LocalAlloc(LPTR, need * sizeof(WCHAR));
    if (!buf) return NULL;
    GetEnvironmentVariableW(name, buf, need);
    return buf;
}

/* Allocate a new heap buffer that holds the concatenation of N wide
 * strings. Returns NULL on alloc failure; the caller LocalFree()s. */
static LPWSTR launcher_concat_w(int count, ...) {
    va_list ap;
    size_t total = 1;  /* trailing NUL */
    va_start(ap, count);
    for (int i = 0; i < count; ++i) {
        LPCWSTR s = va_arg(ap, LPCWSTR);
        if (s) total += wcslen(s);
    }
    va_end(ap);

    LPWSTR out = (LPWSTR)LocalAlloc(LPTR, total * sizeof(WCHAR));
    if (!out) return NULL;
    out[0] = L'\0';

    va_start(ap, count);
    for (int i = 0; i < count; ++i) {
        LPCWSTR s = va_arg(ap, LPCWSTR);
        if (s) wcscat_s(out, total, s);
    }
    va_end(ap);
    return out;
}

/* Build and apply the environment block for the child process. We mutate
 * the launcher's own environment (CreateProcessW with lpEnvironment=NULL
 * inherits it) so this is the simplest correct way to forward env vars
 * without parsing the inherited block by hand. The launcher process is
 * short-lived: it spawns Python and exits, so polluting its own env has no
 * lasting effect.
 *
 * IMPORTANT: only the *child* sees these vars in practice. We never call
 * SetEnvironmentVariable from a long-running parent. */
static BOOL launcher_setup_env(LPCWSTR app_root) {
    LPWSTR runtime    = launcher_concat_w(2, app_root, L"\\runtime");
    LPWSTR tools      = launcher_concat_w(2, app_root, L"\\tools");
    LPWSTR pyhome     = launcher_concat_w(2, app_root, L"\\python");
    LPWSTR pypandoc   = launcher_concat_w(2, app_root, L"\\python\\Lib\\site-packages\\pypandoc\\files");
    LPWSTR pluginsAll = launcher_concat_w(2, app_root, L"\\python\\Lib\\site-packages\\PySide6\\plugins");
    LPWSTR pluginsPlat= launcher_concat_w(2, app_root, L"\\python\\Lib\\site-packages\\PySide6\\plugins\\platforms");
    LPWSTR llamaServer= launcher_concat_w(2, app_root, L"\\tools\\llama-server.exe");
    LPWSTR fontconfig = launcher_concat_w(2, app_root, L"\\runtime\\etc\\fonts");
    LPWSTR gi_typelib = launcher_concat_w(2, app_root, L"\\runtime\\girepository-1.0");
    LPWSTR pypandocExe= launcher_concat_w(2, app_root, L"\\python\\Lib\\site-packages\\pypandoc\\files\\pandoc.exe");

    LPWSTR cache_dir  = NULL;
    LPWSTR localapp   = launcher_getenv_w(L"LOCALAPPDATA");
    if (localapp) {
        cache_dir = launcher_concat_w(2, localapp, L"\\report-anonymizer\\cache");
    }

    /* PATH: prepend runtime;tools;python;pypandoc to the inherited PATH so
     * that DLLs from app\runtime\ resolve before anything system-wide. */
    LPWSTR inheritedPath = launcher_getenv_w(L"PATH");
    LPWSTR newPath = launcher_concat_w(9,
        runtime,    L";",
        tools,      L";",
        pyhome,     L";",
        pypandoc,   L";",
        inheritedPath ? inheritedPath : L"");

    SetEnvironmentVariableW(L"PATH", newPath);
    SetEnvironmentVariableW(L"PYTHONHOME", pyhome);
    /* E5: blank out any inherited PYTHONPATH so the user's system Python
     * never leaks into ours. */
    SetEnvironmentVariableW(L"PYTHONPATH", NULL);
    SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");

    /* Qt: explicit plugin paths so qwindows.dll / qsvg.dll load reliably. */
    SetEnvironmentVariableW(L"QT_QPA_PLATFORM_PLUGIN_PATH", pluginsPlat);
    SetEnvironmentVariableW(L"QT_PLUGIN_PATH",              pluginsAll);

    /* llama-server.exe path (read by anonymize.server_manager). */
    SetEnvironmentVariableW(L"LLAMA_SERVER_BIN", llamaServer);

    /* pypandoc: pin the bundled pandoc.exe so shutil.which can never find a
     * system pandoc that happens to be on PATH (plan §B3). */
    SetEnvironmentVariableW(L"PYPANDOC_PANDOC", pypandocExe);

    /* GLib / Pango / fontconfig: only set when the bundle actually shipped
     * the support tree. Setting them to a non-existent path would break
     * fontconfig fallback. */
    DWORD attr = GetFileAttributesW(fontconfig);
    if (attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY)) {
        SetEnvironmentVariableW(L"FONTCONFIG_PATH", fontconfig);
    }
    attr = GetFileAttributesW(gi_typelib);
    if (attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY)) {
        SetEnvironmentVariableW(L"GI_TYPELIB_PATH", gi_typelib);
    }

    if (cache_dir) {
        /* Lazily ensure the cache directory exists. Ignore errors -- the
         * child process will recreate it on demand if needed. */
        CreateDirectoryW(cache_dir, NULL);
        SetEnvironmentVariableW(L"XDG_CACHE_HOME", cache_dir);
    }

    /* Free heap buffers (env vars have been copied into the process block). */
    if (runtime)     LocalFree(runtime);
    if (tools)       LocalFree(tools);
    if (pyhome)      LocalFree(pyhome);
    if (pypandoc)    LocalFree(pypandoc);
    if (pluginsAll)  LocalFree(pluginsAll);
    if (pluginsPlat) LocalFree(pluginsPlat);
    if (llamaServer) LocalFree(llamaServer);
    if (fontconfig)  LocalFree(fontconfig);
    if (gi_typelib)  LocalFree(gi_typelib);
    if (pypandocExe) LocalFree(pypandocExe);
    if (cache_dir)   LocalFree(cache_dir);
    if (localapp)    LocalFree(localapp);
    if (inheritedPath) LocalFree(inheritedPath);
    if (newPath)     LocalFree(newPath);
    return TRUE;
}

/* Quote a single argv element so that the child sees it as one logical
 * token even if it contains spaces. Allocates a new buffer (LocalFree). */
static LPWSTR launcher_quote_arg(LPCWSTR arg) {
    size_t n = wcslen(arg);
    /* Worst case: every char needs a backslash escape + 2 quotes + NUL. */
    size_t cap = n * 2 + 3;
    LPWSTR out = (LPWSTR)LocalAlloc(LPTR, cap * sizeof(WCHAR));
    if (!out) return NULL;

    size_t o = 0;
    int needs_quotes = (n == 0) || (wcspbrk(arg, L" \t\"") != NULL);
    if (needs_quotes) out[o++] = L'"';

    size_t backslashes = 0;
    for (size_t i = 0; i < n; ++i) {
        WCHAR c = arg[i];
        if (c == L'\\') {
            backslashes++;
        } else if (c == L'"') {
            for (size_t b = 0; b < backslashes * 2 + 1; ++b) out[o++] = L'\\';
            out[o++] = L'"';
            backslashes = 0;
        } else {
            for (size_t b = 0; b < backslashes; ++b) out[o++] = L'\\';
            backslashes = 0;
            out[o++] = c;
        }
    }
    if (needs_quotes) {
        for (size_t b = 0; b < backslashes * 2; ++b) out[o++] = L'\\';
        out[o++] = L'"';
    } else {
        for (size_t b = 0; b < backslashes; ++b) out[o++] = L'\\';
    }
    out[o] = L'\0';
    return out;
}

/* Build the command line "python.exe" target_args... appended_argv...
 * Returns a heap buffer (LocalFree). */
static LPWSTR launcher_build_cmdline(LPCWSTR python_exe,
                                     LPCWSTR const *target_args,
                                     int target_argc,
                                     int forward_argc,
                                     LPCWSTR *forward_argv) {
    LPWSTR qpy = launcher_quote_arg(python_exe);
    size_t cap = (qpy ? wcslen(qpy) : 0) + 1;

    LPWSTR *qtarget = NULL;
    if (target_argc > 0) {
        qtarget = (LPWSTR *)LocalAlloc(LPTR, (size_t)target_argc * sizeof(LPWSTR));
        for (int i = 0; i < target_argc; ++i) {
            qtarget[i] = launcher_quote_arg(target_args[i]);
            if (qtarget[i]) cap += wcslen(qtarget[i]) + 1;
        }
    }

    LPWSTR *qfwd = NULL;
    if (forward_argc > 0) {
        qfwd = (LPWSTR *)LocalAlloc(LPTR, (size_t)forward_argc * sizeof(LPWSTR));
        for (int i = 0; i < forward_argc; ++i) {
            qfwd[i] = launcher_quote_arg(forward_argv[i]);
            if (qfwd[i]) cap += wcslen(qfwd[i]) + 1;
        }
    }

    LPWSTR cmd = (LPWSTR)LocalAlloc(LPTR, cap * sizeof(WCHAR));
    if (!cmd) goto cleanup;
    cmd[0] = L'\0';
    wcscat_s(cmd, cap, qpy);
    for (int i = 0; i < target_argc; ++i) {
        if (!qtarget[i]) continue;
        wcscat_s(cmd, cap, L" ");
        wcscat_s(cmd, cap, qtarget[i]);
    }
    for (int i = 0; i < forward_argc; ++i) {
        if (!qfwd[i]) continue;
        wcscat_s(cmd, cap, L" ");
        wcscat_s(cmd, cap, qfwd[i]);
    }

cleanup:
    if (qpy) LocalFree(qpy);
    if (qtarget) {
        for (int i = 0; i < target_argc; ++i) if (qtarget[i]) LocalFree(qtarget[i]);
        LocalFree(qtarget);
    }
    if (qfwd) {
        for (int i = 0; i < forward_argc; ++i) if (qfwd[i]) LocalFree(qfwd[i]);
        LocalFree(qfwd);
    }
    return cmd;
}

/* Spawn the child and wait. Returns the child exit code, or 127 on failure
 * to launch (mirrors POSIX convention). */
static int launcher_run_child(LPCWSTR python_exe,
                              LPCWSTR const *target_args,
                              int target_argc,
                              int forward_argc,
                              LPCWSTR *forward_argv,
                              LPCWSTR cwd) {
    LPWSTR cmd = launcher_build_cmdline(python_exe, target_args, target_argc,
                                        forward_argc, forward_argv);
    if (!cmd) return 127;

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    BOOL ok = CreateProcessW(
        python_exe,
        cmd,
        NULL, NULL,
        TRUE,                /* inherit handles (stdio for CLI subsystem) */
        0,
        NULL,                /* inherit current (already mutated) env block */
        cwd,
        &si, &pi);

    LocalFree(cmd);

    if (!ok) {
        DWORD err = GetLastError();
        WCHAR msg[256];
        _snwprintf_s(msg, 256, _TRUNCATE,
                     L"Failed to launch Python (CreateProcessW error %lu).\n"
                     L"Path: %ls",
                     err, python_exe);
        /* For the GUI subsystem this is the only place the user ever sees
         * the error. CLI builds inherit stderr so we also emit there. */
        OutputDebugStringW(msg);
        MessageBoxW(NULL, msg, L"Report Anonymizer", MB_ICONERROR | MB_OK);
        return 127;
    }

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}

#endif /* REPORT_ANONYMIZER_LAUNCHER_COMMON_H */
