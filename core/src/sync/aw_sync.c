#include "aw_sync.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#include <math.h>
#include <errno.h>

#ifdef AW_HAVE_PPS
#include <fcntl.h>
#include <unistd.h>
#include <sys/timepps.h>
#endif

struct aw_sync_ctx {
    uint8_t          source;
    char             device[256];
    aw_sync_status_t status;
#ifdef AW_HAVE_PPS
    int              pps_fd;
    pps_handle_t     pps_handle;
#endif
};

static double _clock_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

aw_sync_ctx_t *aw_sync_init(uint8_t source, const char *device)
{
    aw_sync_ctx_t *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) return NULL;

    ctx->source             = source;
    ctx->status.source      = source;
    ctx->status.offset      = 0.0;
    ctx->status.jitter      = (source == AW_SYNC_SOFTWARE) ? 1e-6 : 1e-9;
    ctx->status.last_sync   = 0.0;

    if (device)
        strncpy(ctx->device, device, sizeof(ctx->device) - 1);

#ifdef AW_HAVE_PPS
    ctx->pps_fd = -1;

    if (source == AW_SYNC_PPS || source == AW_SYNC_GPSDO) {
        if (!device || device[0] == '\0') {
            fprintf(stderr, "aw_sync: PPS/GPSDO requires a device path\n");
            free(ctx);
            return NULL;
        }
        ctx->pps_fd = open(device, O_RDWR);
        if (ctx->pps_fd < 0) {
            perror("aw_sync: open PPS device");
            free(ctx);
            return NULL;
        }
        if (time_pps_create(ctx->pps_fd, &ctx->pps_handle) < 0) {
            perror("aw_sync: time_pps_create");
            close(ctx->pps_fd);
            free(ctx);
            return NULL;
        }
        /* kernel PPS binding is optional — continue without it if unavailable */
        time_pps_kcbind(ctx->pps_handle, PPS_KC_HARDPPS,
                        PPS_CAPTUREASSERT, PPS_TSFMT_TSPEC);
    }
#else
    if (source == AW_SYNC_PPS || source == AW_SYNC_GPSDO) {
        fprintf(stderr,
            "aw_sync: built without sys/timepps.h — PPS/GPSDO unavailable.\n"
            "  Install linux-pps-dev (or equivalent) and rebuild.\n");
        free(ctx);
        return NULL;
    }
#endif

    return ctx;
}

void aw_sync_destroy(aw_sync_ctx_t *ctx)
{
    if (!ctx) return;
#ifdef AW_HAVE_PPS
    if (ctx->pps_fd >= 0) {
        time_pps_destroy(ctx->pps_handle);
        close(ctx->pps_fd);
    }
#endif
    free(ctx);
}

int aw_sync_calibrate(aw_sync_ctx_t *ctx)
{
    if (ctx->source == AW_SYNC_SOFTWARE || ctx->source == AW_SYNC_NTP) {
        ctx->status.last_sync = _clock_now();
        return 0;
    }

#ifdef AW_HAVE_PPS
    if (ctx->source == AW_SYNC_PPS || ctx->source == AW_SYNC_GPSDO) {
        struct timespec timeout = { .tv_sec = 2, .tv_nsec = 0 };
        pps_info_t info;
        if (time_pps_fetch(ctx->pps_handle, PPS_TSFMT_TSPEC, &info, &timeout) < 0) {
            perror("aw_sync: time_pps_fetch");
            return -1;
        }
        double pps_ts = (double)info.assert_timestamp.tv_sec
                      + (double)info.assert_timestamp.tv_nsec * 1e-9;
        /*
         * The PPS pulse fires at integer-second boundaries.  The system
         * clock recorded pps_ts at that edge.  The signed offset of the
         * system clock from the true second boundary is:
         *   offset = pps_ts - round(pps_ts)
         *
         * Subtracting this from CLOCK_REALTIME in aw_sync_now() aligns
         * subsequent timestamps to the PPS grid.
         */
        double nearest_sec = floor(pps_ts + 0.5);
        ctx->status.offset    = pps_ts - nearest_sec;
        ctx->status.jitter    = 100e-9;
        ctx->status.last_sync = pps_ts;
        return 0;
    }
#endif

    return -1;
}

aw_sync_status_t aw_sync_status(const aw_sync_ctx_t *ctx)
{
    return ctx->status;
}

double aw_sync_now(const aw_sync_ctx_t *ctx)
{
    return _clock_now() - ctx->status.offset;
}
