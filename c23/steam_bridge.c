// steam_bridge.c — comprehensive steam.exe replacement for amphetamine
//
// Replaces Proton's steam.exe which loads games in-process (breaking
// GetModuleFileNameW for LOVE/PhysFS). This version:
//   1. Initializes Steam IPC (lsteamclient bridge)
//   2. Creates expected named objects (events, window, registry)
//   3. Spawns game via CreateProcessW (separate process)
//   4. Handles Steam DRM handshake
//   5. Waits for child exit

#include <windows.h>
#include <winternl.h>
#include <stdio.h>

// Steam DRM semaphore names (typo is intentional — matches Proton)
#define STEAM_DIPC_CONSUME "STEAM_DIPC_CONSUME"
#define STEAM_DIPC_PRODUCE "SREAM_DIPC_PRODUCE"

// Wine extension: make this a system process
typedef NTSTATUS (WINAPI *pNtSetInformationProcess)(HANDLE, ULONG, PVOID, ULONG);

static HANDLE g_child_process = NULL;

// Steam window thread — some games probe for this window
static DWORD WINAPI steam_window_thread(LPVOID param)
{
    WNDCLASSA wc = {
        .lpfnWndProc = DefWindowProcA,
        .hInstance = GetModuleHandleA(NULL),
        .lpszClassName = "vguiPopupWindow"
    };
    RegisterClassA(&wc);
    CreateWindowExA(0, "vguiPopupWindow", "Steam",
                    WS_POPUP, 40, 40, 1, 1, NULL, NULL, wc.hInstance, NULL);
    MSG msg;
    while (GetMessageA(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    return 0;
}

// Steam DRM handshake thread
static DWORD WINAPI steam_drm_thread(LPVOID param)
{
    HANDLE consume = CreateSemaphoreA(NULL, 0, 512, STEAM_DIPC_CONSUME);
    HANDLE produce = CreateSemaphoreA(NULL, 1, 512, STEAM_DIPC_PRODUCE);
    if (!consume || !produce) return 0;

    while (WaitForSingleObject(g_child_process, 0) == WAIT_TIMEOUT) {
        if (WaitForSingleObject(consume, 100) == WAIT_OBJECT_0) {
            ReleaseSemaphore(produce, 1, NULL);
        }
    }
    CloseHandle(consume);
    CloseHandle(produce);
    return 0;
}

// Initialize lsteamclient bridge via steamclient_init_registry
static void init_lsteamclient(void)
{
    HMODULE hmod = LoadLibraryW(L"lsteamclient.dll");
    if (!hmod) return;

    typedef void (*pfn_init_registry)(void);
    pfn_init_registry init_reg = (pfn_init_registry)GetProcAddress(hmod, "steamclient_init_registry");
    if (init_reg) init_reg();
}

// Create Steam named events games expect
static void create_steam_events(void)
{
    CreateEventA(NULL, FALSE, FALSE, "Steam3Master_SharedMemLock");
    CreateEventA(NULL, FALSE, FALSE, "Global\\Valve_SteamIPC_Class");
}

// Write PID to registry for games that check ActiveProcess
static void write_steam_pid(void)
{
    DWORD pid = GetCurrentProcessId();
    RegSetKeyValueA(HKEY_CURRENT_USER,
                    "Software\\Valve\\Steam\\ActiveProcess",
                    "pid", REG_DWORD, &pid, sizeof(pid));
}

// Create libraryfolders.vdf from environment
static void setup_steam_files(void)
{
    char path[MAX_PATH];
    char *client_path = getenv("STEAM_COMPAT_CLIENT_INSTALL_PATH");
    if (!client_path) return;

    CreateDirectoryA("C:\\Program Files (x86)\\Steam", NULL);
    CreateDirectoryA("C:\\Program Files (x86)\\Steam\\config", NULL);
    CreateDirectoryA("C:\\Program Files (x86)\\Steam\\steamapps", NULL);

    snprintf(path, sizeof(path),
             "C:\\Program Files (x86)\\Steam\\steamapps\\libraryfolders.vdf");

    HANDLE f = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return;

    char buf[4096];
    int len = snprintf(buf, sizeof(buf),
        "\"libraryfolders\"\n{\n}\n");
    DWORD written;
    WriteFile(f, buf, len, &written, NULL);
    CloseHandle(f);
}

int wmain(int argc, wchar_t *argv[])
{
    if (argc < 2) return 1;

    // Phase 1: Steam IPC init
    create_steam_events();
    CreateThread(NULL, 0, steam_window_thread, NULL, 0, NULL);
    write_steam_pid();
    init_lsteamclient();
    setup_steam_files();

    // Phase 2: Build command line from argv[1..] (game exe + its args)
    wchar_t cmdline[32768];
    cmdline[0] = 0;
    for (int i = 1; i < argc; i++) {
        if (i > 1) wcscat(cmdline, L" ");
        int needs_quote = wcschr(argv[i], L' ') != NULL;
        if (needs_quote) wcscat(cmdline, L"\"");
        wcscat(cmdline, argv[i]);
        if (needs_quote) wcscat(cmdline, L"\"");
    }

    // Phase 3: Spawn game as child process
    STARTUPINFOW si = { .cb = sizeof(si) };
    PROCESS_INFORMATION pi = {};

    if (!CreateProcessW(NULL, cmdline, NULL, NULL, TRUE,
                        0, NULL, NULL, &si, &pi))
        return 2;

    g_child_process = pi.hProcess;

    // Phase 4: Steam DRM handshake
    CreateThread(NULL, 0, steam_drm_thread, NULL, 0, NULL);

    // Phase 5: Make this a system process (Wine stays alive)
    HMODULE ntdll = GetModuleHandleA("ntdll.dll");
    if (ntdll) {
        pNtSetInformationProcess pSetInfo =
            (pNtSetInformationProcess)GetProcAddress(ntdll, "NtSetInformationProcess");
        if (pSetInfo) {
            HANDLE wait_handle = NULL;
            pSetInfo(GetCurrentProcess(), 1000, &wait_handle, sizeof(wait_handle));
        }
    }

    // Phase 6: Wait for game to exit
    WaitForSingleObject(pi.hProcess, INFINITE);

    DWORD exit_code = 0;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)exit_code;
}
