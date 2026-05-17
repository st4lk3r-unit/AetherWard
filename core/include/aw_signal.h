#ifndef AW_SIGNAL_H
#define AW_SIGNAL_H

#include "aw_types.h"
#include <stddef.h>

/* ── RSSI sliding-window smoother ───────────────────────────────────────── */

typedef struct {
    float  *buf;
    int     size;
    int     head;
    int     count;
    double  sum;
} aw_rssi_smoother_t;

aw_rssi_smoother_t *aw_rssi_smoother_create(int window);
void                aw_rssi_smoother_destroy(aw_rssi_smoother_t *s);
float               aw_rssi_smoother_update(aw_rssi_smoother_t *s, float rssi);

/* ── Power spectrum ─────────────────────────────────────────────────────── */

typedef struct {
    double *freqs;   /* centre frequency of each bin, Hz */
    float  *power;   /* power per bin, dBm               */
    int     n_bins;
} aw_spectrum_t;

/*
 * Compute power spectrum from interleaved IQ float samples.
 * Caller owns and must free the returned struct via aw_spectrum_free().
 */
aw_spectrum_t *aw_spectrum_compute(const float *iq, size_t n_samples,
                                   double sample_rate, double centre_freq);
void           aw_spectrum_free(aw_spectrum_t *s);

/* ── Log-distance path-loss model ───────────────────────────────────────── */

/*
 * Estimate source distance from received power using the log-distance model:
 *   RSSI(d) = rssi_at_ref_dbm - 10 * path_loss_exp * log10(d / ref_dist_m)
 *
 * rssi_at_ref_dbm is the measured or calibrated RSSI at ref_dist_m — it
 * implicitly absorbs TX power, antenna gains, and any site-specific offset.
 * Returns distance in metres.
 */
double aw_path_loss_distance(float rssi_dbm,
                              float path_loss_exp,
                              float ref_dist_m,
                              float rssi_at_ref_dbm);

#endif /* AW_SIGNAL_H */
