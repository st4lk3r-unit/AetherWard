#ifndef AW_CAPTURE_H
#define AW_CAPTURE_H

#include "aw_types.h"

typedef void (*aw_frame_cb_t)(const aw_frame_t *frame, void *user);

typedef struct aw_capture_ctx aw_capture_ctx_t;

aw_capture_ctx_t *aw_capture_open(const char *iface, aw_frame_cb_t cb, void *user);
int               aw_capture_set_channel(aw_capture_ctx_t *ctx, int channel);
int               aw_capture_set_frequency(aw_capture_ctx_t *ctx, double hz);
int               aw_capture_start(aw_capture_ctx_t *ctx);
void              aw_capture_stop(aw_capture_ctx_t *ctx);
void              aw_capture_close(aw_capture_ctx_t *ctx);

/*
 * Distribute channels across N capture contexts so that every channel
 * is covered by exactly one antenna and nothing is scanned twice.
 */
typedef struct {
    char  antenna_id[64];
    int  *channels;
    int   n_channels;
} aw_chan_assign_t;

int aw_capture_assign_channels(aw_capture_ctx_t **ctxs, int n_ctx,
                                aw_chan_assign_t  *assigns);

#endif /* AW_CAPTURE_H */
