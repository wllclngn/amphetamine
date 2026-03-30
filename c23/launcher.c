// quark launcher — C23 port of rust/src/launcher.rs
//
// Steam calls: ./proton <verb> <exe> [args...]
// Sets up the Wine prefix, deploys DXVK/VKD3D, bridges Steam client,
// then launches wine with WINESERVER pointing at triskelion.
//
// For the kernel module path (/dev/triskelion), the launcher is the ONLY
// userspace component — there is no daemon. Wine processes communicate
// directly with the kernel module via ioctls.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <time.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/syscall.h>
#include <linux/limits.h>

// ---------------------------------------------------------------------------
// C23 features used throughout:
//   constexpr (scalar constants), nullptr, bool/true/false as keywords,
//   static_assert without message, typeof, [[nodiscard]], [[maybe_unused]],
//   __VA_OPT__ in variadic macros, unreachable()
// ---------------------------------------------------------------------------

// Logging — matches Rust's log_info!/log_error!/log_warn!/log_verbose!
static bool g_verbose;

#define LOG_INFO(fmt, ...)    fprintf(stderr, "[quark] " fmt "\n" __VA_OPT__(,) __VA_ARGS__)
#define LOG_ERROR(fmt, ...)   fprintf(stderr, "[quark] ERROR: " fmt "\n" __VA_OPT__(,) __VA_ARGS__)
#define LOG_WARN(fmt, ...)    fprintf(stderr, "[quark] WARN: " fmt "\n" __VA_OPT__(,) __VA_ARGS__)
#define LOG_VERBOSE(fmt, ...) do { if (g_verbose) fprintf(stderr, \
    "[quark] " fmt "\n" __VA_OPT__(,) __VA_ARGS__); } while(0)

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

static const char *const DXVK_DLLS[] = { "d3d11", "d3d10core", "d3d9", "dxgi" };
constexpr int NDXVK = 4;

static const char *const VKD3D_DLLS[] = { "d3d12", "d3d12core" };
constexpr int NVKD3D = 2;

typedef struct { const char *name; const char *mode; } DllOverride;

static const DllOverride BASE_OVERRIDES[] = {
    { "steam.exe",        "b"   },
    { "dotnetfx35.exe",   "b"   },
    { "dotnetfx35setup.exe", "b" },
    { "beclient.dll",     "b,n" },
    { "beclient_x64.dll", "b,n" },
    { "xinput1_1.dll",    "b"   },
    { "xinput1_2.dll",    "b"   },
    { "xinput1_3.dll",    "b"   },
    { "xinput1_4.dll",    "b"   },
    { "xinput9_1_0.dll",  "b"   },
    { "xinputuap.dll",    "b"   },
    { "winebth.sys",      "d"   },
};
constexpr int NBASE_OVERRIDES = sizeof(BASE_OVERRIDES) / sizeof(BASE_OVERRIDES[0]);

typedef struct { const char *src_name; const char *dst_name; } SteamFile;

static const SteamFile STEAM_CLIENT_FILES[] = {
    { "steamclient64.dll",        "steamclient64.dll"        },
    { "steamclient.dll",          "steamclient.dll"          },
    { "GameOverlayRenderer64.dll","GameOverlayRenderer64.dll"},
    { "SteamService.exe",         "steam.exe"                },
    { "Steam.dll",                "Steam.dll"                },
};
constexpr int NSTEAM_FILES = sizeof(STEAM_CLIENT_FILES) / sizeof(STEAM_CLIENT_FILES[0]);

static const char CACHE_FILE[] = ".triskelion_deployed";

static const char *const SAVE_SCAN_DIRS[] = {
    "AppData/Roaming", "AppData/Local", "AppData/LocalLow", "Documents"
};
constexpr int NSAVE_SCAN = 4;

static const char *const SAVE_SKIP_DIRS[] = { "Microsoft", "Temp" };
constexpr int NSAVE_SKIP = 2;

// ---------------------------------------------------------------------------
// Globals (set once at startup, never mutated)
// ---------------------------------------------------------------------------

static char g_home[PATH_MAX];
static char g_self_exe[PATH_MAX];

// ---------------------------------------------------------------------------
// Dynamic string buffer
// ---------------------------------------------------------------------------

typedef struct {
    char  *data;
    size_t len;
    size_t cap;
} StrBuf;

static void sb_init(StrBuf *sb) {
    sb->data = nullptr;
    sb->len = 0;
    sb->cap = 0;
}

static void sb_ensure(StrBuf *sb, size_t extra) {
    size_t need = sb->len + extra + 1;
    if (need <= sb->cap) return;
    size_t newcap = sb->cap ? sb->cap * 2 : 256;
    while (newcap < need) newcap *= 2;
    sb->data = realloc(sb->data, newcap);
    sb->cap = newcap;
}

static void sb_append(StrBuf *sb, const char *s) {
    size_t slen = strlen(s);
    sb_ensure(sb, slen);
    memcpy(sb->data + sb->len, s, slen);
    sb->len += slen;
    sb->data[sb->len] = '\0';
}

static void sb_append_sep(StrBuf *sb, const char *s, char sep) {
    if (sb->len > 0) {
        sb_ensure(sb, 1);
        sb->data[sb->len++] = sep;
    }
    sb_append(sb, s);
}

static void sb_free(StrBuf *sb) {
    free(sb->data);
    sb->data = nullptr;
    sb->len = sb->cap = 0;
}

// ---------------------------------------------------------------------------
// Environment variable list
// ---------------------------------------------------------------------------

constexpr int MAX_ENV = 64;

typedef struct {
    const char *key;
    char       *value;   // heap-allocated
} EnvPair;

typedef struct {
    EnvPair pairs[MAX_ENV];
    int     count;
} EnvList;

static void env_init(EnvList *el) { el->count = 0; }

static void env_add(EnvList *el, const char *key, const char *value) {
    if (el->count >= MAX_ENV) return;
    el->pairs[el->count].key   = key;
    el->pairs[el->count].value = strdup(value);
    el->count++;
}

static void env_free(EnvList *el) {
    for (int i = 0; i < el->count; i++)
        free(el->pairs[i].value);
    el->count = 0;
}

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------

static uint64_t elapsed_ms(const struct timespec *start) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    int64_t ds  = now.tv_sec  - start->tv_sec;
    int64_t dns = now.tv_nsec - start->tv_nsec;
    return (uint64_t)(ds * 1000 + dns / 1000000);
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

static void ensure_stdio_fds(void) {
    // Wine's socketpair() picks the lowest free fd. Under Steam's reaper,
    // stdin may be closed -> fd 0 is free -> socketpair gets fd 0 -> child's
    // set_stdio_fd overwrites it with /dev/null -> WINESERVERSOCKET destroyed.
    for (int fd = 0; fd < 3; fd++) {
        if (fcntl(fd, F_GETFD) == -1)
            open("/dev/null", O_RDWR);
    }
}

static bool file_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0;
}

static bool is_directory(const char *path) {
    struct stat st;
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

[[maybe_unused]]
static bool is_symlink(const char *path) {
    struct stat st;
    return lstat(path, &st) == 0 && S_ISLNK(st.st_mode);
}

static bool makedirs(const char *path) {
    char tmp[PATH_MAX];
    size_t len = strlen(path);
    if (len >= PATH_MAX) return false;
    memcpy(tmp, path, len + 1);

    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST) return false;
            *p = '/';
        }
    }
    return mkdir(tmp, 0755) == 0 || errno == EEXIST;
}

static bool copy_file(const char *src, const char *dst) {
    int sfd = open(src, O_RDONLY);
    if (sfd < 0) return false;

    int dfd = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (dfd < 0) { close(sfd); return false; }

    char buf[65536];
    ssize_t n;
    bool ok = true;
    while ((n = read(sfd, buf, sizeof(buf))) > 0) {
        if (write(dfd, buf, (size_t)n) != n) { ok = false; break; }
    }
    if (n < 0) ok = false;
    close(sfd);
    close(dfd);
    return ok;
}

static ssize_t read_file_string(const char *path, char *buf, size_t bufsize) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, bufsize - 1);
    close(fd);
    if (n < 0) return -1;
    buf[n] = '\0';
    return n;
}

// Check if dst exists and matches src (same size, dst mtime >= src mtime)
static bool file_matches(const char *src, const char *dst) {
    struct stat ss, ds;
    if (stat(src, &ss) != 0 || stat(dst, &ds) != 0) return false;
    return ss.st_size == ds.st_size && ss.st_mtime <= ds.st_mtime;
}

static bool remove_dir_all(const char *path) {
    DIR *d = opendir(path);
    if (!d) return rmdir(path) == 0;

    struct dirent *ent;
    while ((ent = readdir(d)) != nullptr) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0)
            continue;
        char child[PATH_MAX];
        snprintf(child, sizeof(child), "%s/%s", path, ent->d_name);
        struct stat st;
        if (lstat(child, &st) == 0 && S_ISDIR(st.st_mode))
            remove_dir_all(child);
        else
            unlink(child);
    }
    closedir(d);
    return rmdir(path) == 0;
}

static int strcasecmp_wrapper(const char *a, const char *b) {
    return strcasecmp(a, b);
}

// ---------------------------------------------------------------------------
// Deployment cache
// ---------------------------------------------------------------------------

typedef struct {
    uint64_t wine_hash;
    uint64_t dxvk_hash;
    uint64_t vkd3d_hash;
    uint64_t steam_hash;
} DeployCache;

static bool cache_load(const char *pfx, DeployCache *c) {
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s", pfx, CACHE_FILE);

    char buf[256];
    if (read_file_string(path, buf, sizeof(buf)) < 0) return false;

    // Format: "v3:wine,dxvk,vkd3d,steam"
    if (strncmp(buf, "v3:", 3) != 0) return false;
    char *p = buf + 3;
    char *end;

    c->wine_hash  = strtoull(p, &end, 10); if (*end != ',') return false; p = end + 1;
    c->dxvk_hash  = strtoull(p, &end, 10); if (*end != ',') return false; p = end + 1;
    c->vkd3d_hash = strtoull(p, &end, 10); if (*end != ',') return false; p = end + 1;
    c->steam_hash = strtoull(p, &end, 10);
    return true;
}

static void cache_save(const char *pfx, const DeployCache *c) {
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s", pfx, CACHE_FILE);

    char buf[256];
    int n = snprintf(buf, sizeof(buf), "v3:%lu,%lu,%lu,%lu",
        (unsigned long)c->wine_hash, (unsigned long)c->dxvk_hash,
        (unsigned long)c->vkd3d_hash, (unsigned long)c->steam_hash);

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        LOG_WARN("Cannot write deployment cache: %s", strerror(errno));
        return;
    }
    write(fd, buf, (size_t)n);
    close(fd);
}

// ---------------------------------------------------------------------------
// Directory hash — quick metadata fingerprint
// ---------------------------------------------------------------------------

static uint64_t dir_hash(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    uint64_t dev   = (uint64_t)st.st_dev;
    uint64_t ino   = (uint64_t)st.st_ino;
    uint64_t mtime = (uint64_t)st.st_mtime;
    return (dev * 6364136223846793005ULL)
         ^ (ino * 1442695040888963407ULL)
         ^ mtime;
}

// ---------------------------------------------------------------------------
// Fast recursive directory copy (getdents64 + hardlinks)
// ---------------------------------------------------------------------------

static uint32_t copy_dir_fast(const char *src, const char *dst) {
    int fd = open(src, O_RDONLY | O_DIRECTORY);
    if (fd < 0) {
        LOG_ERROR("copy_dir_fast: failed to open %s", src);
        return 0;
    }
    if (!makedirs(dst)) {
        LOG_ERROR("copy_dir_fast: cannot create %s: %s", dst, strerror(errno));
        close(fd);
        return 0;
    }

    uint8_t buf[32 * 1024];
    uint32_t count = 0;

    for (;;) {
        long nread = syscall(SYS_getdents64, fd, buf, sizeof(buf));
        if (nread <= 0) break;

        size_t pos = 0;
        while (pos < (size_t)nread) {
            // linux_dirent64: d_ino(8) + d_off(8) + d_reclen(2) + d_type(1) + d_name(...)
            uint16_t d_reclen;
            memcpy(&d_reclen, buf + pos + 16, 2);
            uint8_t d_type = buf[pos + 18];
            const char *name = (const char *)(buf + pos + 19);

            if (strcmp(name, ".") == 0 || strcmp(name, "..") == 0) {
                pos += d_reclen;
                continue;
            }

            char src_path[PATH_MAX], dst_path[PATH_MAX];
            snprintf(src_path, sizeof(src_path), "%s/%s", src, name);
            snprintf(dst_path, sizeof(dst_path), "%s/%s", dst, name);

            // DT_UNKNOWN on some filesystems — fall back to stat
            if (d_type == 0) {
                struct stat st;
                if (lstat(src_path, &st) != 0) { pos += d_reclen; continue; }
                if (S_ISDIR(st.st_mode))       d_type = 4;
                else if (S_ISLNK(st.st_mode))  d_type = 10;
                else                            d_type = 8;
            }

            if (d_type == 4) {
                // Directory: recurse
                count += copy_dir_fast(src_path, dst_path);
            } else if (d_type == 10) {
                // Symlink: resolve against source tree, create absolute symlinks
                bool needs_fix = true;
                struct stat lst;
                if (lstat(dst_path, &lst) == 0) {
                    if (S_ISLNK(lst.st_mode)) {
                        struct stat tgt;
                        needs_fix = stat(dst_path, &tgt) != 0; // broken link
                    }
                }
                if (needs_fix) {
                    unlink(dst_path);
                    char real[PATH_MAX];
                    if (realpath(src_path, real) != nullptr) {
                        symlink(real, dst_path);
                    } else {
                        char link_target[PATH_MAX];
                        ssize_t ln = readlink(src_path, link_target, sizeof(link_target) - 1);
                        if (ln > 0) {
                            link_target[ln] = '\0';
                            symlink(link_target, dst_path);
                        }
                    }
                    count++;
                }
            } else {
                // Regular file: hardlink first, copy fallback
                if (!file_exists(dst_path)) {
                    if (link(src_path, dst_path) == 0) {
                        count++;
                    } else if (copy_file(src_path, dst_path)) {
                        count++;
                    } else {
                        LOG_WARN("copy_dir_fast: failed to deploy %s", src_path);
                    }
                }
            }
            pos += d_reclen;
        }
    }

    close(fd);
    return count;
}

// ---------------------------------------------------------------------------
// Prefix setup
// ---------------------------------------------------------------------------

static void setup_prefix(const char *wine_dir, const char *pfx,
                         const char *wine64, const char *self_exe)
{
    char default_pfx[PATH_MAX], sys_reg[PATH_MAX];
    snprintf(default_pfx, sizeof(default_pfx), "%s/share/default_pfx", wine_dir);
    snprintf(sys_reg, sizeof(sys_reg), "%s/system.reg", pfx);

    if (is_directory(default_pfx)) {
        bool fresh = !file_exists(sys_reg);
        LOG_INFO("%s prefix from template...", fresh ? "Setting up" : "Repairing");

        uint32_t n = copy_dir_fast(default_pfx, pfx);
        if (n > 0) LOG_INFO("Prefix: %u files deployed", n);

        // dosdevices symlinks
        char dosdevices[PATH_MAX], c_link[PATH_MAX], z_link[PATH_MAX];
        snprintf(dosdevices, sizeof(dosdevices), "%s/dosdevices", pfx);
        makedirs(dosdevices);

        snprintf(c_link, sizeof(c_link), "%s/c:", dosdevices);
        snprintf(z_link, sizeof(z_link), "%s/z:", dosdevices);
        if (!file_exists(c_link)) symlink("../drive_c", c_link);
        if (!file_exists(z_link)) symlink("/", z_link);

        // Prevent Wine from re-updating prefix every launch
        char wine_inf[PATH_MAX];
        snprintf(wine_inf, sizeof(wine_inf), "%s/share/wine/wine.inf", wine_dir);
        struct stat inf_st;
        if (stat(wine_inf, &inf_st) == 0) {
            char ts_file[PATH_MAX], ts_buf[32];
            snprintf(ts_file, sizeof(ts_file), "%s/.update-timestamp", pfx);
            snprintf(ts_buf, sizeof(ts_buf), "%ld", (long)inf_st.st_mtime);
            int tfd = open(ts_file, O_WRONLY | O_CREAT | O_TRUNC, 0644);
            if (tfd >= 0) { write(tfd, ts_buf, strlen(ts_buf)); close(tfd); }
        }
    } else if (file_exists(sys_reg)) {
        LOG_INFO("Prefix exists, skipping wineboot (no template needed)");
        char dosdevices[PATH_MAX], c_link[PATH_MAX], z_link[PATH_MAX];
        snprintf(dosdevices, sizeof(dosdevices), "%s/dosdevices", pfx);
        makedirs(dosdevices);
        snprintf(c_link, sizeof(c_link), "%s/c:", dosdevices);
        snprintf(z_link, sizeof(z_link), "%s/z:", dosdevices);
        if (!file_exists(c_link)) symlink("../drive_c", c_link);
        if (!file_exists(z_link)) symlink("/", z_link);
    } else {
        LOG_INFO("No default_pfx template, running wineboot --init...");
        pid_t pid = fork();
        if (pid == 0) {
            setenv("WINEPREFIX", pfx, 1);
            setenv("WINESERVER", self_exe, 1);
            setenv("WINEDEBUG", "-all", 1);
            setenv("DISPLAY", "", 1);
            execlp(wine64, wine64, "wineboot", "--init", nullptr);
            _exit(127);
        }
        if (pid > 0) {
            int status;
            waitpid(pid, &status, 0);
            if (WIFEXITED(status) && WEXITSTATUS(status) != 0)
                LOG_ERROR("wineboot --init failed with exit code %d", WEXITSTATUS(status));
        } else {
            LOG_ERROR("Failed to fork for wineboot: %s", strerror(errno));
        }
    }
}

// ---------------------------------------------------------------------------
// Registry injection
// ---------------------------------------------------------------------------

static void inject_registry_keys(const char *pfx) {
    char sys_reg[PATH_MAX];
    snprintf(sys_reg, sizeof(sys_reg), "%s/system.reg", pfx);

    char check_buf[16384];
    if (read_file_string(sys_reg, check_buf, sizeof(check_buf)) >= 0) {
        if (strstr(check_buf, "VC\\\\Runtimes\\\\x64") != nullptr)
            return; // Already injected
    }

    static const char keys[] =
        "\n\n"
        "[Software\\\\Microsoft\\\\VisualStudio\\\\14.0\\\\VC\\\\Runtimes\\\\x64] 1772204972\n"
        "#time=1dca7fb13d11a48\n"
        "\"Installed\"=dword:00000001\n"
        "\"Major\"=dword:0000000e\n"
        "\"Minor\"=dword:00000024\n"
        "\"Bld\"=dword:00007280\n"
        "\n"
        "[Software\\\\WOW6432Node\\\\Microsoft\\\\VisualStudio\\\\14.0\\\\VC\\\\Runtimes\\\\x86] 1772204972\n"
        "#time=1dca7fb13d11a48\n"
        "\"Installed\"=dword:00000001\n"
        "\"Major\"=dword:0000000e\n"
        "\"Minor\"=dword:00000024\n"
        "\"Bld\"=dword:00007280\n"
        "\n"
        "[Software\\\\Microsoft\\\\NET Framework Setup\\\\NDP\\\\v4\\\\Full] 1772204972\n"
        "#time=1dca7fb13d11a48\n"
        "\"Install\"=dword:00000001\n"
        "\"Release\"=dword:00080ff4\n"
        "\"Version\"=\"4.8.09037\"\n"
        "\n";

    int fd = open(sys_reg, O_WRONLY | O_APPEND);
    if (fd < 0) {
        LOG_WARN("Cannot open system.reg for registry injection: %s", strerror(errno));
        return;
    }
    write(fd, keys, strlen(keys));
    close(fd);
    LOG_INFO("Registry: injected VC++ and .NET Framework keys");
}

// ---------------------------------------------------------------------------
// DLL deployment
// ---------------------------------------------------------------------------

static void deploy_dlls(const char *src_dir, const char *dst_dir,
                        const char *const *dlls, int ndlls,
                        const char *label, bool deployed[])
{
    if (!is_directory(src_dir)) {
        LOG_WARN("%s: source dir not found (%s)", label, src_dir);
        return;
    }
    makedirs(dst_dir);

    uint32_t copied = 0, skipped = 0;
    for (int i = 0; i < ndlls; i++) {
        char src[PATH_MAX], dst[PATH_MAX];
        snprintf(src, sizeof(src), "%s/%s.dll", src_dir, dlls[i]);
        snprintf(dst, sizeof(dst), "%s/%s.dll", dst_dir, dlls[i]);

        if (!file_exists(src)) continue;

        if (file_matches(src, dst)) {
            skipped++;
            deployed[i] = true;
            continue;
        }

        unlink(dst); // Remove old — hardlinked files from Proton may be read-only
        if (copy_file(src, dst)) {
            deployed[i] = true;
            copied++;
        } else {
            LOG_WARN("%s: failed to copy %s.dll", label, dlls[i]);
        }
    }
    if (copied > 0)
        LOG_VERBOSE("%s: deployed %u DLLs (%u already current)", label, copied, skipped);
    else if (skipped > 0)
        LOG_VERBOSE("%s: %u DLLs already current", label, skipped);
}

static void deploy_dxvk(const char *wine_dir, const char *pfx, bool deployed[]) {
    char src64[PATH_MAX], sys32[PATH_MAX];
    snprintf(src64, sizeof(src64), "%s/lib/wine/dxvk/x86_64-windows", wine_dir);
    snprintf(sys32, sizeof(sys32), "%s/drive_c/windows/system32", pfx);
    deploy_dlls(src64, sys32, DXVK_DLLS, NDXVK, "DXVK", deployed);

    char src32[PATH_MAX], syswow64[PATH_MAX];
    snprintf(src32, sizeof(src32), "%s/lib/wine/dxvk/i386-windows", wine_dir);
    snprintf(syswow64, sizeof(syswow64), "%s/drive_c/windows/syswow64", pfx);
    bool dummy[NDXVK] = {false};
    deploy_dlls(src32, syswow64, DXVK_DLLS, NDXVK, "DXVK-32", dummy);
}

static void deploy_vkd3d(const char *wine_dir, const char *pfx, bool deployed[]) {
    char src64[PATH_MAX], sys32[PATH_MAX];
    snprintf(src64, sizeof(src64), "%s/lib/wine/vkd3d-proton/x86_64-windows", wine_dir);
    snprintf(sys32, sizeof(sys32), "%s/drive_c/windows/system32", pfx);
    deploy_dlls(src64, sys32, VKD3D_DLLS, NVKD3D, "VKD3D", deployed);

    char src32[PATH_MAX], syswow64[PATH_MAX];
    snprintf(src32, sizeof(src32), "%s/lib/wine/vkd3d-proton/i386-windows", wine_dir);
    snprintf(syswow64, sizeof(syswow64), "%s/drive_c/windows/syswow64", pfx);
    bool dummy[NVKD3D] = {false};
    deploy_dlls(src32, syswow64, VKD3D_DLLS, NVKD3D, "VKD3D-32", dummy);
}

// ---------------------------------------------------------------------------
// Steam client integration
// ---------------------------------------------------------------------------

static void deploy_steam_client(const char *steam_dir, const char *pfx) {
    char legacy[PATH_MAX];
    snprintf(legacy, sizeof(legacy), "%s/legacycompat", steam_dir);
    if (!is_directory(legacy)) {
        LOG_WARN("Steam legacycompat not found at %s", legacy);
        return;
    }

    char steam_pfx[PATH_MAX];
    snprintf(steam_pfx, sizeof(steam_pfx), "%s/drive_c/Program Files (x86)/Steam", pfx);
    makedirs(steam_pfx);

    uint32_t copied = 0, skipped = 0;
    for (int i = 0; i < NSTEAM_FILES; i++) {
        char src[PATH_MAX], dst[PATH_MAX];
        snprintf(src, sizeof(src), "%s/%s", legacy, STEAM_CLIENT_FILES[i].src_name);
        snprintf(dst, sizeof(dst), "%s/%s", steam_pfx, STEAM_CLIENT_FILES[i].dst_name);
        if (!file_exists(src)) continue;
        if (file_matches(src, dst)) { skipped++; continue; }
        unlink(dst);
        if (copy_file(src, dst)) copied++;
        else LOG_WARN("Steam client: failed to copy %s", STEAM_CLIENT_FILES[i].src_name);
    }
    if (copied > 0)
        LOG_VERBOSE("Steam client: deployed %u files (%u already current)", copied, skipped);
    else if (skipped > 0)
        LOG_VERBOSE("Steam client: %u files already current", skipped);
}

static void deploy_steam_exe(const char *proton_dir, const char *pfx) {
    char cached[PATH_MAX];
    snprintf(cached, sizeof(cached), "%s/.local/share/quark/steam.exe", g_home);

    const char *subdirs[] = { "drive_c/windows/system32", "drive_c/windows/syswow64" };
    for (int i = 0; i < 2; i++) {
        char dst[PATH_MAX];
        snprintf(dst, sizeof(dst), "%s/%s/steam.exe", pfx, subdirs[i]);
        if (file_exists(dst)) continue;

        char dst_dir[PATH_MAX];
        snprintf(dst_dir, sizeof(dst_dir), "%s/%s", pfx, subdirs[i]);

        if (file_exists(cached)) {
            makedirs(dst_dir);
            if (copy_file(cached, dst))
                LOG_INFO("steam.exe: deployed to %s", dst_dir);
            else
                LOG_WARN("steam.exe: failed to copy to %s", dst_dir);
            continue;
        }
        if (proton_dir != nullptr) {
            const char *arch[] = { "x86_64-windows", "i386-windows" };
            for (int j = 0; j < 2; j++) {
                char src[PATH_MAX];
                snprintf(src, sizeof(src), "%s/lib/wine/%s/steam.exe", proton_dir, arch[j]);
                if (file_exists(src)) {
                    makedirs(dst_dir);
                    if (copy_file(src, dst))
                        LOG_INFO("steam.exe: deployed to %s", dst_dir);
                    break;
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

static bool has_wine_bin(const char *dir) {
    char p[PATH_MAX];
    snprintf(p, sizeof(p), "%s/bin/wine64", dir);
    if (file_exists(p)) return true;
    snprintf(p, sizeof(p), "%s/bin/wine", dir);
    return file_exists(p);
}

static void wine_binary(const char *dir, char *out, size_t outsize) {
    snprintf(out, outsize, "%s/bin/wine64", dir);
    if (file_exists(out)) return;
    snprintf(out, outsize, "%s/bin/wine", dir);
}

static void find_wine(char *out, size_t outsize) {
    // 1. TRISKELION_WINE_DIR env var
    const char *env = getenv("TRISKELION_WINE_DIR");
    if (env && has_wine_bin(env)) {
        snprintf(out, outsize, "%s", env);
        return;
    }

    // 2. Quark's staged wine (self_exe dir)
    char self_dir[PATH_MAX];
    snprintf(self_dir, sizeof(self_dir), "%s", g_self_exe);
    char *slash = strrchr(self_dir, '/');
    if (slash) {
        *slash = '\0';
        char ntdll_check[PATH_MAX];
        snprintf(ntdll_check, sizeof(ntdll_check), "%s/lib/wine/x86_64-unix/ntdll.so", self_dir);
        if (has_wine_bin(self_dir) && file_exists(ntdll_check)) {
            snprintf(out, outsize, "%s", self_dir);
            return;
        }
    }

    // 3. Proton Experimental
    char proton_exp[PATH_MAX];
    snprintf(proton_exp, sizeof(proton_exp),
        "%s/.steam/root/steamapps/common/Proton - Experimental/files", g_home);
    if (has_wine_bin(proton_exp)) {
        snprintf(out, outsize, "%s", proton_exp);
        return;
    }

    // 4. Any Proton version
    char common[PATH_MAX];
    snprintf(common, sizeof(common), "%s/.steam/root/steamapps/common", g_home);
    DIR *d = opendir(common);
    if (d) {
        struct dirent *ent;
        while ((ent = readdir(d)) != nullptr) {
            if (strncmp(ent->d_name, "Proton", 6) != 0) continue;
            char files[PATH_MAX];
            snprintf(files, sizeof(files), "%s/%s/files", common, ent->d_name);
            if (has_wine_bin(files)) {
                snprintf(out, outsize, "%s", files);
                closedir(d);
                return;
            }
        }
        closedir(d);
    }

    // 5. System Wine
    if (has_wine_bin("/usr")) {
        snprintf(out, outsize, "/usr");
        return;
    }

    snprintf(out, outsize, "/nonexistent/wine");
}

static bool find_proton_files(char *out, size_t outsize) {
    char proton_exp[PATH_MAX];
    snprintf(proton_exp, sizeof(proton_exp),
        "%s/.steam/root/steamapps/common/Proton - Experimental/files", g_home);
    char wine_check[PATH_MAX];
    snprintf(wine_check, sizeof(wine_check), "%s/lib/wine", proton_exp);
    if (is_directory(wine_check)) {
        snprintf(out, outsize, "%s", proton_exp);
        return true;
    }

    char common[PATH_MAX];
    snprintf(common, sizeof(common), "%s/.steam/root/steamapps/common", g_home);
    DIR *d = opendir(common);
    if (d) {
        struct dirent *ent;
        while ((ent = readdir(d)) != nullptr) {
            if (strncmp(ent->d_name, "Proton", 6) != 0) continue;
            char files[PATH_MAX];
            snprintf(files, sizeof(files), "%s/%s/files", common, ent->d_name);
            char wcheck[PATH_MAX];
            snprintf(wcheck, sizeof(wcheck), "%s/lib/wine", files);
            if (is_directory(wcheck)) {
                snprintf(out, outsize, "%s", files);
                closedir(d);
                return true;
            }
        }
        closedir(d);
    }
    return false;
}

static void find_steam_dir(char *out, size_t outsize) {
    const char *env = getenv("STEAM_COMPAT_CLIENT_INSTALL_PATH");
    if (env) {
        char check[PATH_MAX];
        snprintf(check, sizeof(check), "%s/linux64/steamclient.so", env);
        if (file_exists(check)) {
            snprintf(out, outsize, "%s", env);
            return;
        }
    }

    char steam[PATH_MAX], check[PATH_MAX];
    snprintf(steam, sizeof(steam), "%s/.steam/root", g_home);
    snprintf(check, sizeof(check), "%s/linux64/steamclient.so", steam);
    if (file_exists(check)) { snprintf(out, outsize, "%s", steam); return; }

    snprintf(steam, sizeof(steam), "%s/.local/share/Steam", g_home);
    snprintf(check, sizeof(check), "%s/linux64/steamclient.so", steam);
    if (file_exists(check)) { snprintf(out, outsize, "%s", steam); return; }

    LOG_WARN("Steam directory not found — game may not connect to Steam");
    snprintf(out, outsize, "%s/.steam/root", g_home);
}

// ---------------------------------------------------------------------------
// Environment construction
// ---------------------------------------------------------------------------

static void build_env_vars(EnvList *env,
    const char *wine_dir, const char *proton_dir,
    const char *steam_dir, const char *pfx, const char *self_exe,
    const bool *dxvk_deployed, const bool *vkd3d_deployed,
    bool trace, bool shader_cache_enabled)
{
    const char *cur_path = getenv("PATH");
    if (!cur_path) cur_path = "";
    const char *cur_ld = getenv("LD_LIBRARY_PATH");
    if (!cur_ld) cur_ld = "";

    // Self dir for quark lib
    char self_dir[PATH_MAX];
    snprintf(self_dir, sizeof(self_dir), "%s", self_exe);
    char *sl = strrchr(self_dir, '/');
    if (sl) *sl = '\0';

    // WINEDLLPATH
    StrBuf dll_path;
    sb_init(&dll_path);
    {
        char amp_lib[PATH_MAX];
        snprintf(amp_lib, sizeof(amp_lib), "%s/lib", self_dir);
        if (is_directory(amp_lib))
            sb_append_sep(&dll_path, amp_lib, ':');

        char wine_vkd3d[PATH_MAX];
        snprintf(wine_vkd3d, sizeof(wine_vkd3d), "%s/lib/vkd3d", wine_dir);
        if (is_directory(wine_vkd3d))
            sb_append_sep(&dll_path, wine_vkd3d, ':');

        char wine_dll[PATH_MAX];
        snprintf(wine_dll, sizeof(wine_dll), "%s/lib/wine", wine_dir);
        sb_append_sep(&dll_path, wine_dll, ':');

        if (proton_dir) {
            char pvkd3d[PATH_MAX], pdll[PATH_MAX];
            snprintf(pvkd3d, sizeof(pvkd3d), "%s/lib/vkd3d", proton_dir);
            snprintf(pdll, sizeof(pdll), "%s/lib/wine", proton_dir);
            if (is_directory(pvkd3d)) sb_append_sep(&dll_path, pvkd3d, ':');
            if (is_directory(pdll) && strcmp(pdll, wine_dll) != 0)
                sb_append_sep(&dll_path, pdll, ':');
        }
    }

    // LD_LIBRARY_PATH
    StrBuf ld_path;
    sb_init(&ld_path);
    {
        if (proton_dir) {
            char pnative[PATH_MAX];
            snprintf(pnative, sizeof(pnative), "%s/lib/x86_64-linux-gnu", proton_dir);
            if (is_directory(pnative)) sb_append_sep(&ld_path, pnative, ':');
        }

        char wnative[PATH_MAX];
        snprintf(wnative, sizeof(wnative), "%s/lib/x86_64-linux-gnu", wine_dir);
        if (is_directory(wnative)) sb_append_sep(&ld_path, wnative, ':');

        char slinux64[PATH_MAX];
        snprintf(slinux64, sizeof(slinux64), "%s/linux64", steam_dir);
        if (is_directory(slinux64)) sb_append_sep(&ld_path, slinux64, ':');

        char wine_lib[PATH_MAX];
        snprintf(wine_lib, sizeof(wine_lib), "%s/lib", wine_dir);
        sb_append_sep(&ld_path, wine_lib, ':');

        // x86_64-unix dirs for ntdll.so NEEDED resolution
        char amp_unix[PATH_MAX];
        snprintf(amp_unix, sizeof(amp_unix), "%s/lib/wine/x86_64-unix", self_dir);
        if (is_directory(amp_unix)) sb_append_sep(&ld_path, amp_unix, ':');

        char wine_unix[PATH_MAX];
        snprintf(wine_unix, sizeof(wine_unix), "%s/lib/wine/x86_64-unix", wine_dir);
        if (is_directory(wine_unix)) sb_append_sep(&ld_path, wine_unix, ':');

        if (proton_dir) {
            char plib[PATH_MAX];
            snprintf(plib, sizeof(plib), "%s/lib", proton_dir);
            if (is_directory(plib) && strcmp(plib, wine_lib) != 0)
                sb_append_sep(&ld_path, plib, ':');
        }

        if (cur_ld[0]) sb_append_sep(&ld_path, cur_ld, ':');
    }

    // WINEDLLOVERRIDES
    StrBuf overrides;
    sb_init(&overrides);
    for (int i = 0; i < NBASE_OVERRIDES; i++) {
        char entry[256];
        snprintf(entry, sizeof(entry), "%s=%s", BASE_OVERRIDES[i].name, BASE_OVERRIDES[i].mode);
        sb_append_sep(&overrides, entry, ';');
    }
    for (int i = 0; i < NDXVK; i++) {
        if (dxvk_deployed[i]) {
            char entry[64];
            snprintf(entry, sizeof(entry), "%s=n", DXVK_DLLS[i]);
            sb_append_sep(&overrides, entry, ';');
        }
    }
    for (int i = 0; i < NVKD3D; i++) {
        if (vkd3d_deployed[i]) {
            char entry[64];
            snprintf(entry, sizeof(entry), "%s=n", VKD3D_DLLS[i]);
            sb_append_sep(&overrides, entry, ';');
        }
    }

    // PATH
    char path_buf[PATH_MAX * 2];
    snprintf(path_buf, sizeof(path_buf), "%s/bin:%s", wine_dir, cur_path);

    // WINELOADER
    char wine64[PATH_MAX];
    wine_binary(wine_dir, wine64, sizeof(wine64));

    // WINEDEBUG
    const char *winedebug_env = getenv("WINEDEBUG");
    const char *winedebug = winedebug_env ? winedebug_env
        : (trace ? "+server,+timestamp" : "+module,+server");

    env_add(env, "WINEPREFIX",       pfx);
    env_add(env, "WINESERVER",       self_exe);
    env_add(env, "WINELOADER",       wine64);
    env_add(env, "WINEDLLPATH",      dll_path.data  ? dll_path.data  : "");
    env_add(env, "PATH",             path_buf);
    env_add(env, "LD_LIBRARY_PATH",  ld_path.data   ? ld_path.data   : "");
    env_add(env, "WINEDEBUG",        winedebug);
    env_add(env, "WINEDLLOVERRIDES", overrides.data  ? overrides.data : "");
    env_add(env, "DXVK_LOG_LEVEL",   "none");
    env_add(env, "VKD3D_DEBUG",      "none");
    env_add(env, "WINE_LARGE_ADDRESS_AWARE", "1");
    env_add(env, "DXVK_ASYNC",       "1");
    env_add(env, "VKD3D_CONFIG",     "shader_cache");

    // ntsync
    if (file_exists("/dev/ntsync"))
        env_add(env, "WINE_NTSYNC", "1");
    env_add(env, "WINEFSYNC", "1");
    env_add(env, "WINEESYNC", "1");

    // Steam Input
    const char *gamepad = getenv("SteamVirtualGamepadInfo_Proton");
    if (gamepad) env_add(env, "SteamVirtualGamepadInfo", gamepad);

    // Native Wayland — always on.
    env_add(env, "WINE_ENABLE_WAYLAND", "1");

    // Shader cache
    if (shader_cache_enabled) {
        char sc_dir[PATH_MAX];
        snprintf(sc_dir, sizeof(sc_dir), "%s/shader_cache", pfx);
        makedirs(sc_dir);

        env_add(env, "DXVK_SHADER_CACHE_PATH",  sc_dir);
        env_add(env, "VKD3D_SHADER_CACHE_PATH",  sc_dir);

        bool is_nvidia = file_exists("/proc/driver/nvidia/version");
        if (is_nvidia) {
            env_add(env, "__GL_SHADER_DISK_CACHE",              "1");
            env_add(env, "__GL_SHADER_DISK_CACHE_PATH",         sc_dir);
            env_add(env, "__GL_SHADER_DISK_CACHE_SIZE",         "10737418240");
            env_add(env, "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "1");
        } else {
            env_add(env, "MESA_SHADER_CACHE_DIR",         sc_dir);
            env_add(env, "MESA_SHADER_CACHE_MAX_SIZE",    "10G");
            env_add(env, "MESA_DISK_CACHE_SINGLE_FILE",   "1");
            env_add(env, "RADV_PERFTEST",                  "gpl");
        }
    }

    sb_free(&dll_path);
    sb_free(&ld_path);
    sb_free(&overrides);
}

static void parse_env_config(const char *self_exe, EnvList *env) {
    char self_dir[PATH_MAX];
    snprintf(self_dir, sizeof(self_dir), "%s", self_exe);
    char *sl = strrchr(self_dir, '/');
    if (!sl) return;
    *sl = '\0';

    char config_path[PATH_MAX];
    snprintf(config_path, sizeof(config_path), "%s/env_config", self_dir);

    char buf[8192];
    if (read_file_string(config_path, buf, sizeof(buf)) < 0) return;

    int loaded = 0;
    char *line = strtok(buf, "\n");
    while (line) {
        while (*line == ' ' || *line == '\t') line++;
        if (*line == '\0' || *line == '#') { line = strtok(nullptr, "\n"); continue; }

        char *eq = strchr(line, '=');
        if (!eq) {
            LOG_WARN("env_config: ignoring malformed line: %s", line);
            line = strtok(nullptr, "\n");
            continue;
        }
        *eq = '\0';
        char *key = line;
        char *value = eq + 1;
        // Trim
        char *kend = key + strlen(key) - 1;
        while (kend > key && (*kend == ' ' || *kend == '\t')) *kend-- = '\0';
        while (*value == ' ' || *value == '\t') value++;

        if (*key) { env_add(env, key, value); loaded++; }
        line = strtok(nullptr, "\n");
    }
    if (loaded > 0)
        LOG_INFO("env_config: loaded %d custom variable(s)", loaded);
}

// ---------------------------------------------------------------------------
// Save data protection
// ---------------------------------------------------------------------------

static void count_files_recursive(const char *dir, uint32_t *files, uint64_t *bytes) {
    DIR *d = opendir(dir);
    if (!d) return;
    struct dirent *ent;
    while ((ent = readdir(d)) != nullptr) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        char path[PATH_MAX];
        snprintf(path, sizeof(path), "%s/%s", dir, ent->d_name);
        struct stat st;
        if (stat(path, &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) {
            count_files_recursive(path, files, bytes);
        } else {
            (*files)++;
            *bytes += (uint64_t)st.st_size;
        }
    }
    closedir(d);
}

static void copy_save_recursive(const char *src, const char *dst) {
    makedirs(dst);
    DIR *d = opendir(src);
    if (!d) return;
    struct dirent *ent;
    while ((ent = readdir(d)) != nullptr) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        char sp[PATH_MAX], dp[PATH_MAX];
        snprintf(sp, sizeof(sp), "%s/%s", src, ent->d_name);
        snprintf(dp, sizeof(dp), "%s/%s", dst, ent->d_name);
        if (is_directory(sp))
            copy_save_recursive(sp, dp);
        else if (!copy_file(sp, dp))
            LOG_WARN("save backup: failed to copy %s", sp);
    }
    closedir(d);
}

static bool snapshot_save_data(const char *pfx, char *backup_out, size_t outsize) {
    char user_dir[PATH_MAX];
    snprintf(user_dir, sizeof(user_dir), "%s/drive_c/users/steamuser", pfx);
    if (!is_directory(user_dir)) return false;

    // backup_dir is parent of pfx (STEAM_COMPAT_DATA_PATH)/save_backup
    char backup_dir[PATH_MAX];
    snprintf(backup_dir, sizeof(backup_dir), "%s", pfx);
    char *sl = strrchr(backup_dir, '/');
    if (!sl) return false;
    *sl = '\0';
    size_t blen = strlen(backup_dir);
    snprintf(backup_dir + blen, sizeof(backup_dir) - blen, "/save_backup");

    if (is_directory(backup_dir))
        remove_dir_all(backup_dir);

    uint32_t total_files = 0;
    uint64_t total_bytes = 0;

    for (int s = 0; s < NSAVE_SCAN; s++) {
        char scan_dir[PATH_MAX];
        snprintf(scan_dir, sizeof(scan_dir), "%s/%s", user_dir, SAVE_SCAN_DIRS[s]);
        if (!is_directory(scan_dir)) continue;

        DIR *d = opendir(scan_dir);
        if (!d) continue;
        struct dirent *ent;
        while ((ent = readdir(d)) != nullptr) {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;

            bool skip = false;
            for (int k = 0; k < NSAVE_SKIP; k++) {
                if (strcasecmp_wrapper(ent->d_name, SAVE_SKIP_DIRS[k]) == 0)
                    { skip = true; break; }
            }
            if (skip) continue;

            char entry_path[PATH_MAX];
            snprintf(entry_path, sizeof(entry_path), "%s/%s", scan_dir, ent->d_name);
            if (!is_directory(entry_path)) continue;

            uint32_t files = 0;
            uint64_t bytes = 0;
            count_files_recursive(entry_path, &files, &bytes);
            if (files == 0) continue;

            char dst[PATH_MAX];
            snprintf(dst, sizeof(dst), "%s/%s/%s", backup_dir, SAVE_SCAN_DIRS[s], ent->d_name);
            copy_save_recursive(entry_path, dst);
            total_files += files;
            total_bytes += bytes;
        }
        closedir(d);
    }

    if (total_files == 0) return false;

    // Write manifest
    char manifest[PATH_MAX];
    snprintf(manifest, sizeof(manifest), "%s/manifest.txt", backup_dir);
    char mbuf[128];
    int mn = snprintf(mbuf, sizeof(mbuf), "%u files, %lu bytes\n",
        total_files, (unsigned long)total_bytes);
    int mfd = open(manifest, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (mfd >= 0) { write(mfd, mbuf, (size_t)mn); close(mfd); }

    LOG_INFO("save data snapshot: %u files, %lu bytes", total_files, (unsigned long)total_bytes);
    snprintf(backup_out, outsize, "%s", backup_dir);
    return true;
}

static void restore_missing_files(const char *backup, const char *original,
                                  uint32_t *restored, uint32_t *unchanged)
{
    DIR *d = opendir(backup);
    if (!d) return;
    struct dirent *ent;
    while ((ent = readdir(d)) != nullptr) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        char bp[PATH_MAX], op[PATH_MAX];
        snprintf(bp, sizeof(bp), "%s/%s", backup, ent->d_name);
        snprintf(op, sizeof(op), "%s/%s", original, ent->d_name);

        if (is_directory(bp)) {
            restore_missing_files(bp, op, restored, unchanged);
        } else {
            if (file_exists(op)) {
                (*unchanged)++;
            } else {
                char parent[PATH_MAX];
                snprintf(parent, sizeof(parent), "%s", op);
                char *sl2 = strrchr(parent, '/');
                if (sl2) { *sl2 = '\0'; makedirs(parent); }
                if (copy_file(bp, op)) (*restored)++;
                else LOG_WARN("save restore: failed to restore %s", op);
            }
        }
    }
    closedir(d);
}

static void restore_save_data(const char *pfx, const char *backup_dir) {
    if (!is_directory(backup_dir)) return;

    char user_dir[PATH_MAX];
    snprintf(user_dir, sizeof(user_dir), "%s/drive_c/users/steamuser", pfx);

    uint32_t restored = 0, unchanged = 0;
    for (int s = 0; s < NSAVE_SCAN; s++) {
        char backup_scan[PATH_MAX];
        snprintf(backup_scan, sizeof(backup_scan), "%s/%s", backup_dir, SAVE_SCAN_DIRS[s]);
        if (!is_directory(backup_scan)) continue;

        DIR *d = opendir(backup_scan);
        if (!d) continue;
        struct dirent *ent;
        while ((ent = readdir(d)) != nullptr) {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
            char bp[PATH_MAX], op[PATH_MAX];
            snprintf(bp, sizeof(bp), "%s/%s", backup_scan, ent->d_name);
            if (!is_directory(bp)) continue;
            snprintf(op, sizeof(op), "%s/%s/%s", user_dir, SAVE_SCAN_DIRS[s], ent->d_name);
            restore_missing_files(bp, op, &restored, &unchanged);
        }
        closedir(d);
    }

    LOG_INFO("save data check: %u files restored, %u unchanged", restored, unchanged);
    if (restored > 0) {
        LOG_WARN("%u save files were missing after game exit — restored from backup", restored);
    } else {
        remove_dir_all(backup_dir);
    }
}

static void count_save_data_stats(const char *pfx, uint32_t *files_out, uint64_t *bytes_out) {
    *files_out = 0;
    *bytes_out = 0;
    char user_dir[PATH_MAX];
    snprintf(user_dir, sizeof(user_dir), "%s/drive_c/users/steamuser", pfx);
    if (!is_directory(user_dir)) return;

    for (int s = 0; s < NSAVE_SCAN; s++) {
        char scan_dir[PATH_MAX];
        snprintf(scan_dir, sizeof(scan_dir), "%s/%s", user_dir, SAVE_SCAN_DIRS[s]);
        if (!is_directory(scan_dir)) continue;

        DIR *d = opendir(scan_dir);
        if (!d) continue;
        struct dirent *ent;
        while ((ent = readdir(d)) != nullptr) {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
            bool skip = false;
            for (int k = 0; k < NSAVE_SKIP; k++) {
                if (strcasecmp_wrapper(ent->d_name, SAVE_SKIP_DIRS[k]) == 0)
                    { skip = true; break; }
            }
            if (skip) continue;
            char path[PATH_MAX];
            snprintf(path, sizeof(path), "%s/%s", scan_dir, ent->d_name);
            if (!is_directory(path)) continue;
            count_files_recursive(path, files_out, bytes_out);
        }
        closedir(d);
    }
}

// ---------------------------------------------------------------------------
// Crash dump cleanup
// ---------------------------------------------------------------------------

static uint32_t remove_dumps_recursive(const char *dir) {
    DIR *d = opendir(dir);
    if (!d) return 0;
    uint32_t removed = 0;
    struct dirent *ent;
    while ((ent = readdir(d)) != nullptr) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        char path[PATH_MAX];
        snprintf(path, sizeof(path), "%s/%s", dir, ent->d_name);
        if (is_directory(path)) {
            removed += remove_dumps_recursive(path);
        } else {
            const char *dot = strrchr(ent->d_name, '.');
            if (dot && (strcasecmp(dot, ".dmp") == 0 || strcasecmp(dot, ".mdmp") == 0)) {
                if (unlink(path) == 0) removed++;
            }
        }
    }
    closedir(d);
    return removed;
}

static void clean_crash_dumps(const char *pfx) {
    char user_dir[PATH_MAX];
    snprintf(user_dir, sizeof(user_dir), "%s/drive_c/users/steamuser", pfx);
    if (!is_directory(user_dir)) return;

    uint32_t removed = 0;
    for (int s = 0; s < NSAVE_SCAN; s++) {
        char dir[PATH_MAX];
        snprintf(dir, sizeof(dir), "%s/%s", user_dir, SAVE_SCAN_DIRS[s]);
        if (is_directory(dir)) removed += remove_dumps_recursive(dir);
    }
    if (removed > 0) LOG_INFO("cleaned %u stale crash dump(s)", removed);
}

// ---------------------------------------------------------------------------
// Prometheus launch diagnostics (simplified)
// ---------------------------------------------------------------------------

static void write_launch_prom(
    const char *compat_data, const char *wine_dir, const char *steam_dir,
    const char *pfx, const char *self_exe, const char *game_exe,
    const EnvList *env, uint64_t t_discover, uint64_t t_prefix,
    uint64_t t_dxvk, uint64_t t_steam, uint64_t t_total,
    bool wine_valid, bool dxvk_valid, bool vkd3d_valid, bool steam_valid,
    bool shader_cache_enabled)
{
    makedirs("/tmp/quark");

    // Timestamp for filename
    time_t now = time(nullptr);
    struct tm tm;
    localtime_r(&now, &tm);
    char ts[32];
    strftime(ts, sizeof(ts), "%Y%m%d-%H%M%S", &tm);

    char filename[PATH_MAX];
    snprintf(filename, sizeof(filename), "/tmp/quark/launch-%s.prom", ts);

    FILE *f = fopen(filename, "w");
    if (!f) {
        LOG_WARN("cannot write launch diagnostics: %s", strerror(errno));
        return;
    }

    // System info
    char kernel[256] = "unknown";
    {
        char buf[256];
        if (read_file_string("/proc/version", buf, sizeof(buf)) > 0) {
            char *sp = strchr(buf, ' ');
            if (sp) { sp = strchr(sp + 1, ' '); }
            if (sp) {
                sp++;
                char *end = strchr(sp, ' ');
                if (end) *end = '\0';
                snprintf(kernel, sizeof(kernel), "%s", sp);
            }
        }
    }

    long cpus = sysconf(_SC_NPROCESSORS_ONLN);
    uint64_t ram = (uint64_t)sysconf(_SC_PHYS_PAGES) * (uint64_t)sysconf(_SC_PAGE_SIZE);
    const char *gpu = file_exists("/proc/driver/nvidia/version") ? "nvidia" : "mesa";

    fprintf(f, "# Generated by quark launcher (C23)\n\n");
    fprintf(f, "# HELP quark_system_info System identification\n");
    fprintf(f, "quark_system_info{kernel=\"%s\",gpu_vendor=\"%s\"} 1\n", kernel, gpu);
    fprintf(f, "quark_system_cpu_count %ld\n", cpus);
    fprintf(f, "quark_system_ram_bytes %lu\n", (unsigned long)ram);

    // App info
    const char *app_id = strrchr(compat_data, '/');
    app_id = app_id ? app_id + 1 : compat_data;
    fprintf(f, "\nquark_steam_app_id{app_id=\"%s\"} 1\n", app_id);
    if (game_exe) fprintf(f, "quark_game_executable{path=\"%s\"} 1\n", game_exe);

    // Paths
    fprintf(f, "\nquark_wine_dir{path=\"%s\"} 1\n", wine_dir);
    fprintf(f, "quark_steam_dir{path=\"%s\"} 1\n", steam_dir);
    fprintf(f, "quark_wineserver{path=\"%s\"} 1\n", self_exe);
    fprintf(f, "quark_wineprefix{path=\"%s\"} 1\n", pfx);

    // Cache
    fprintf(f, "\nquark_cache_hit{component=\"wine\"} %d\n", wine_valid);
    fprintf(f, "quark_cache_hit{component=\"dxvk\"} %d\n", dxvk_valid);
    fprintf(f, "quark_cache_hit{component=\"vkd3d\"} %d\n", vkd3d_valid);
    fprintf(f, "quark_cache_hit{component=\"steam\"} %d\n", steam_valid);

    // Timing
    fprintf(f, "\nquark_setup_duration_ms{phase=\"discover\"} %lu\n", (unsigned long)t_discover);
    fprintf(f, "quark_setup_duration_ms{phase=\"prefix\"} %lu\n", (unsigned long)t_prefix);
    fprintf(f, "quark_setup_duration_ms{phase=\"dxvk_vkd3d\"} %lu\n", (unsigned long)t_dxvk);
    fprintf(f, "quark_setup_duration_ms{phase=\"steam_client\"} %lu\n", (unsigned long)t_steam);
    fprintf(f, "quark_setup_duration_ms{phase=\"total\"} %lu\n", (unsigned long)t_total);

    // Shader cache
    fprintf(f, "\nquark_shader_cache_enabled %d\n", shader_cache_enabled);

    // Save data stats
    uint32_t save_files = 0;
    uint64_t save_bytes = 0;
    count_save_data_stats(pfx, &save_files, &save_bytes);
    fprintf(f, "\nquark_save_backup_files %u\n", save_files);
    fprintf(f, "quark_save_backup_bytes %lu\n", (unsigned long)save_bytes);

    // Env vars
    fprintf(f, "\n");
    for (int i = 0; i < env->count; i++) {
        fprintf(f, "quark_env_var{name=\"%s\"} 1\n", env->pairs[i].key);
    }

    fclose(f);

    // Latest symlink
    unlink("/tmp/quark/launch-latest.prom");
    char basename[128];
    snprintf(basename, sizeof(basename), "launch-%s.prom", ts);
    symlink(basename, "/tmp/quark/launch-latest.prom");

    LOG_VERBOSE("launch diagnostics: %s", filename);
}

// ---------------------------------------------------------------------------
// Main launcher entry point
// ---------------------------------------------------------------------------

static int launcher_run(const char *verb, char *const *args, int nargs) {
    ensure_stdio_fds();

    // Quick verbs
    if (strcmp(verb, "getcompatpath") == 0 || strcmp(verb, "getnativepath") == 0) {
        if (nargs > 0) printf("%s\n", args[0]);
        return 0;
    }
    if (strcmp(verb, "installscript") == 0 || strcmp(verb, "runinprefix") == 0) {
        LOG_INFO("%s: no-op (Wine provides these APIs natively)", verb);
        return 0;
    }

    struct timespec t_start;
    clock_gettime(CLOCK_MONOTONIC, &t_start);

    // Phase 1: Locate everything
    char wine_dir[PATH_MAX], steam_dir[PATH_MAX], wine64[PATH_MAX];
    find_wine(wine_dir, sizeof(wine_dir));
    find_steam_dir(steam_dir, sizeof(steam_dir));
    wine_binary(wine_dir, wine64, sizeof(wine64));

    if (!file_exists(wine64)) {
        LOG_ERROR("wine not found at %s", wine64);
        LOG_ERROR("Need Wine binaries. Install Wine from your package manager.");
        return 1;
    }

    char proton_dir[PATH_MAX];
    bool has_proton = find_proton_files(proton_dir, sizeof(proton_dir));

    // DXVK/VKD3D source
    char dxvk_src_dir[PATH_MAX];
    {
        char amp_dxvk[PATH_MAX];
        snprintf(amp_dxvk, sizeof(amp_dxvk),
            "%s/.local/share/Steam/compatibilitytools.d/quark", g_home);
        char check[PATH_MAX];
        snprintf(check, sizeof(check), "%s/lib/wine/dxvk", amp_dxvk);

        if (is_directory(check)) {
            snprintf(dxvk_src_dir, sizeof(dxvk_src_dir), "%s", amp_dxvk);
        } else {
            snprintf(check, sizeof(check), "%s/lib/wine/dxvk", wine_dir);
            if (is_directory(check)) {
                snprintf(dxvk_src_dir, sizeof(dxvk_src_dir), "%s", wine_dir);
            } else if (has_proton) {
                LOG_INFO("DXVK/VKD3D: sourcing from Proton (%s)", proton_dir);
                snprintf(dxvk_src_dir, sizeof(dxvk_src_dir), "%s", proton_dir);
            } else {
                LOG_WARN("DXVK/VKD3D: no source found — games needing D3D may fail");
                snprintf(dxvk_src_dir, sizeof(dxvk_src_dir), "%s", wine_dir);
            }
        }
    }

    const char *compat_data = getenv("STEAM_COMPAT_DATA_PATH");
    if (!compat_data || !*compat_data) {
        LOG_ERROR("STEAM_COMPAT_DATA_PATH not set — not launched from Steam?");
        return 1;
    }

    char pfx[PATH_MAX];
    snprintf(pfx, sizeof(pfx), "%s/pfx", compat_data);
    makedirs(pfx);

    uint64_t t_discover = elapsed_ms(&t_start);

    LOG_INFO("wine64: %s", wine64);
    LOG_INFO("wineserver: %s (triskelion)", g_self_exe);
    LOG_INFO("steam: %s", steam_dir);

    // Per-component cache
    DeployCache cache;
    bool cache_valid = cache_load(pfx, &cache);

    char dxvk_hash_path[PATH_MAX], vkd3d_hash_path[PATH_MAX];
    snprintf(dxvk_hash_path, sizeof(dxvk_hash_path), "%s/lib/wine/dxvk", dxvk_src_dir);
    snprintf(vkd3d_hash_path, sizeof(vkd3d_hash_path), "%s/lib/wine/vkd3d-proton", dxvk_src_dir);
    char steam_legacy[PATH_MAX];
    snprintf(steam_legacy, sizeof(steam_legacy), "%s/legacycompat", steam_dir);

    uint64_t wine_hash  = dir_hash(wine_dir);
    uint64_t dxvk_hash  = dir_hash(dxvk_hash_path);
    uint64_t vkd3d_hash = dir_hash(vkd3d_hash_path);
    uint64_t steam_hash = dir_hash(steam_legacy);

    bool wine_valid  = cache_valid && cache.wine_hash  == wine_hash;
    bool dxvk_valid  = cache_valid && cache.dxvk_hash  == dxvk_hash;
    bool vkd3d_valid = cache_valid && cache.vkd3d_hash == vkd3d_hash;
    bool steam_valid = cache_valid && cache.steam_hash == steam_hash;

    // Phase 2: Prefix setup
    struct timespec t2;
    clock_gettime(CLOCK_MONOTONIC, &t2);
    if (!wine_valid)
        setup_prefix(wine_dir, pfx, wine64, g_self_exe);
    uint64_t t_prefix = elapsed_ms(&t2);

    // Phase 3: Deploy DXVK/VKD3D
    struct timespec t3;
    clock_gettime(CLOCK_MONOTONIC, &t3);

    bool dxvk_deployed[NDXVK] = {false};
    bool vkd3d_deployed[NVKD3D] = {false};
    if (dxvk_valid) {
        for (int i = 0; i < NDXVK; i++) dxvk_deployed[i] = true;
    } else {
        deploy_dxvk(dxvk_src_dir, pfx, dxvk_deployed);
    }
    if (vkd3d_valid) {
        for (int i = 0; i < NVKD3D; i++) vkd3d_deployed[i] = true;
    } else {
        deploy_vkd3d(dxvk_src_dir, pfx, vkd3d_deployed);
    }
    uint64_t t_dxvk = elapsed_ms(&t3);

    // Phase 4: Steam client
    struct timespec t4;
    clock_gettime(CLOCK_MONOTONIC, &t4);
    if (!steam_valid)
        deploy_steam_client(steam_dir, pfx);
    deploy_steam_exe(has_proton ? proton_dir : nullptr, pfx);
    uint64_t t_steam_ms = elapsed_ms(&t4);

    inject_registry_keys(pfx);

    // Save cache if anything changed
    bool all_valid = wine_valid && dxvk_valid && vkd3d_valid && steam_valid;
    if (!all_valid) {
        DeployCache new_cache = { wine_hash, dxvk_hash, vkd3d_hash, steam_hash };
        cache_save(pfx, &new_cache);
        LOG_VERBOSE("deployment cache written");
    } else {
        LOG_VERBOSE("cache hit — skipped all file ops");
    }

    // Phase 5: Build environment
    bool trace = getenv("QUARK_TRACE_OPCODES") != nullptr
              || file_exists("/tmp/quark/TRACE_OPCODES");

    bool shader_cache_enabled = false;
    {
        char self_dir[PATH_MAX], sc_flag[PATH_MAX];
        snprintf(self_dir, sizeof(self_dir), "%s", g_self_exe);
        char *sl = strrchr(self_dir, '/');
        if (sl) {
            *sl = '\0';
            snprintf(sc_flag, sizeof(sc_flag), "%s/shader_cache_enabled", self_dir);
            shader_cache_enabled = file_exists(sc_flag);
        }
    }

    EnvList env;
    env_init(&env);
    build_env_vars(&env, wine_dir, has_proton ? proton_dir : nullptr,
                   steam_dir, pfx, g_self_exe,
                   dxvk_deployed, vkd3d_deployed,
                   trace, shader_cache_enabled);

    EnvList custom_env;
    env_init(&custom_env);
    parse_env_config(g_self_exe, &custom_env);

    uint64_t t_total = elapsed_ms(&t_start);

    // Verbose diagnostics
    if (g_verbose) {
        LOG_VERBOSE("timing: discover=%lums prefix=%lums dxvk=%lums steam=%lums total=%lums",
            (unsigned long)t_discover, (unsigned long)t_prefix,
            (unsigned long)t_dxvk, (unsigned long)t_steam_ms, (unsigned long)t_total);

        const char *game_exe_prom = nullptr;
        if ((strcmp(verb, "waitforexitandrun") == 0 || strcmp(verb, "run") == 0) && nargs > 0)
            game_exe_prom = args[0];

        write_launch_prom(compat_data, wine_dir, steam_dir, pfx, g_self_exe,
            game_exe_prom, &env,
            t_discover, t_prefix, t_dxvk, t_steam_ms, t_total,
            wine_valid, dxvk_valid, vkd3d_valid, steam_valid,
            shader_cache_enabled);
    }

    // Phase 6: Launch
    int ret = 1;
    if (strcmp(verb, "waitforexitandrun") == 0 || strcmp(verb, "run") == 0) {
        if (nargs < 1) {
            LOG_ERROR("No executable specified");
            goto cleanup;
        }
        const char *game_exe = args[0];

        // Skip install script evaluator
        if (strstr(game_exe, "iscriptevaluator") != nullptr) {
            LOG_INFO("skipping install script evaluator (Wine provides these APIs)");
            ret = 0;
            goto cleanup;
        }

        if (!file_exists(game_exe))
            LOG_WARN("game exe not found at %s (may be a Windows path — continuing)", game_exe);

        LOG_INFO("launching: %s", game_exe);

        clean_crash_dumps(pfx);

        // Save data snapshot
        char save_backup[PATH_MAX] = {0};
        bool has_backup = snapshot_save_data(pfx, save_backup, sizeof(save_backup));

        // Build argv for wine64
        int child_argc = 1 + nargs;
        char **child_argv = malloc((size_t)(child_argc + 1) * sizeof(char *));
        child_argv[0] = (char *)wine64;
        for (int i = 0; i < nargs; i++)
            child_argv[i + 1] = args[i];
        child_argv[child_argc] = nullptr;

        // Stderr handling
        int stderr_fd = -1;
        char stderr_log[PATH_MAX] = {0};

        if (trace) {
            makedirs("/tmp/quark");
            stderr_fd = open("/tmp/quark/opcode_trace.log",
                             O_WRONLY | O_CREAT | O_TRUNC, 0644);
        } else if (g_verbose) {
            makedirs("/tmp/quark");
            time_t now2 = time(nullptr);
            struct tm tm2;
            localtime_r(&now2, &tm2);
            char ts2[32];
            strftime(ts2, sizeof(ts2), "%Y%m%d-%H%M%S", &tm2);
            snprintf(stderr_log, sizeof(stderr_log),
                "/tmp/quark/stderr-%s.log", ts2);
            stderr_fd = open(stderr_log, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        }

        pid_t pid = fork();
        if (pid == 0) {
            // Child: set env vars
            for (int i = 0; i < env.count; i++)
                setenv(env.pairs[i].key, env.pairs[i].value, 1);
            for (int i = 0; i < custom_env.count; i++)
                setenv(custom_env.pairs[i].key, custom_env.pairs[i].value, 1);

            if (stderr_fd >= 0) {
                dup2(stderr_fd, STDERR_FILENO);
                close(stderr_fd);
            }

            execvp(wine64, child_argv);
            LOG_ERROR("Failed to exec wine64: %s", strerror(errno));
            _exit(127);
        }

        if (stderr_fd >= 0) close(stderr_fd); // parent closes its copy
        free(child_argv);

        if (pid < 0) {
            LOG_ERROR("Failed to fork: %s", strerror(errno));
            ret = 1;
            goto cleanup;
        }

        int status;
        waitpid(pid, &status, 0);

        // Process exit status
        if (WIFEXITED(status)) {
            int code = WEXITSTATUS(status);
            ret = code;

            if (stderr_log[0]) {
                // Update symlink
                unlink("/tmp/quark/stderr-latest.log");
                char *bn = strrchr(stderr_log, '/');
                if (bn) symlink(bn + 1, "/tmp/quark/stderr-latest.log");

                // Append exit info
                int afd = open(stderr_log, O_WRONLY | O_APPEND);
                if (afd >= 0) {
                    char exit_msg[128];
                    int elen = snprintf(exit_msg, sizeof(exit_msg),
                        "\n[quark] exit: %d (%s)\n",
                        code, code == 0 ? "clean" : "error");
                    write(afd, exit_msg, (size_t)elen);
                    close(afd);
                }
                LOG_VERBOSE("stderr log: %s", stderr_log);
            }
        } else if (WIFSIGNALED(status)) {
            int sig = WTERMSIG(status);
            LOG_WARN("wine64 killed by signal %d", sig);
            ret = 1;

            if (stderr_log[0]) {
                unlink("/tmp/quark/stderr-latest.log");
                char *bn = strrchr(stderr_log, '/');
                if (bn) symlink(bn + 1, "/tmp/quark/stderr-latest.log");

                int afd = open(stderr_log, O_WRONLY | O_APPEND);
                if (afd >= 0) {
                    char exit_msg[128];
                    int elen = snprintf(exit_msg, sizeof(exit_msg),
                        "\n[quark] killed by signal %d\n", sig);
                    write(afd, exit_msg, (size_t)elen);
                    close(afd);
                }
            }
        }

        // Restore save data
        if (has_backup)
            restore_save_data(pfx, save_backup);

        if (strcmp(verb, "waitforexitandrun") == 0)
            sleep(2);

        goto cleanup;
    }

    LOG_ERROR("Unknown verb: %s", verb);
    ret = 1;

cleanup:
    env_free(&env);
    env_free(&custom_env);
    return ret;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

int main(int argc, char *argv[]) {
    // Initialize globals
    g_verbose = getenv("QUARK_VERBOSE") != nullptr;

    const char *home = getenv("HOME");
    if (home)
        snprintf(g_home, sizeof(g_home), "%s", home);
    else
        g_home[0] = '\0';

    ssize_t n = readlink("/proc/self/exe", g_self_exe, sizeof(g_self_exe) - 1);
    if (n > 0) g_self_exe[n] = '\0';
    else snprintf(g_self_exe, sizeof(g_self_exe), "%s", argv[0]);

    // Steam calls: ./proton <verb> <exe> [args...]
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <verb> [exe] [args...]\n", argv[0]);
        fprintf(stderr, "  Verbs: run, waitforexitandrun, getcompatpath, getnativepath\n");
        return 1;
    }

    const char *verb = argv[1];
    return launcher_run(verb, argv + 2, argc - 2);
}
