// noassert.so — LD_PRELOAD shim for Wine under amphetamine.
//
// 1. Suppresses assertion failures in Wine's ntdll unix layer
//    (add_fd_to_cache collision, user_check_not_lock race).
// 2. Forces stderr to line-buffered mode so WINE_TRACE output
//    is flushed per-line even when stderr is redirected to a file.
//    Without this, traces between the last fflush and _exit() are lost.
//
// Build: gcc -shared -fPIC -O2 -o noassert.so noassert.c

#define _GNU_SOURCE
#include <stdio.h>
#include <signal.h>
#include <string.h>

__attribute__((constructor))
static void init(void)
{
    // Line-buffer stderr so Wine traces survive process death.
    setvbuf(stderr, NULL, _IOLBF, 0);

    // Ignore SIGABRT from Wine assertion failures.
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = SIG_IGN;
    sigaction(SIGABRT, &sa, NULL);
}
