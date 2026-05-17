#ifndef AW_SYNC_H
#define AW_SYNC_H

#include "aw_types.h"

/*
 * Sync source, ordered by achievable precision.
 * Choosing the right source is critical for TDOA: 1 ns error ≈ 30 cm.
 */
#define AW_SYNC_SOFTWARE  0   /* OS clock, µs class                    */
#define AW_SYNC_NTP       1   /* NTP-disciplined, ~1 ms                */
#define AW_SYNC_PPS       2   /* GPS PPS via /dev/ppsX, ~100 ns        */
#define AW_SYNC_GPSDO     3   /* GPS-disciplined oscillator, ~1 ns     */

typedef struct {
    uint8_t source;       /* AW_SYNC_*                              */
    double  offset;       /* estimated clock offset vs reference, s */
    double  jitter;       /* 1-sigma jitter, s                      */
    double  last_sync;    /* Unix epoch of last calibration         */
} aw_sync_status_t;

typedef struct aw_sync_ctx aw_sync_ctx_t;

aw_sync_ctx_t    *aw_sync_init(uint8_t source, const char *device);
void              aw_sync_destroy(aw_sync_ctx_t *ctx);
int               aw_sync_calibrate(aw_sync_ctx_t *ctx);
aw_sync_status_t  aw_sync_status(const aw_sync_ctx_t *ctx);
double            aw_sync_now(const aw_sync_ctx_t *ctx);  /* corrected timestamp */

#endif /* AW_SYNC_H */
