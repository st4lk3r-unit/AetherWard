#include "aw_signal.h"

#include <stdlib.h>
#include <string.h>
#include <math.h>

/* ── RSSI smoother ──────────────────────────────────────────────────────── */

aw_rssi_smoother_t *aw_rssi_smoother_create(int window)
{
    if (window < 1) return NULL;
    aw_rssi_smoother_t *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->buf  = calloc(window, sizeof(float));
    if (!s->buf) { free(s); return NULL; }
    s->size = window;
    return s;
}

void aw_rssi_smoother_destroy(aw_rssi_smoother_t *s)
{
    if (!s) return;
    free(s->buf);
    free(s);
}

float aw_rssi_smoother_update(aw_rssi_smoother_t *s, float rssi)
{
    if (s->count == s->size)
        s->sum -= s->buf[s->head];
    else
        s->count++;

    s->buf[s->head] = rssi;
    s->sum += rssi;
    s->head = (s->head + 1) % s->size;
    return (float)(s->sum / s->count);
}

/* ── Power spectrum ─────────────────────────────────────────────────────── */

/*
 * Cooley-Tukey radix-2 FFT on complex float input.
 * n must be a power of 2.  re/im modified in-place.
 */
static void _fft(double *re, double *im, int n)
{
    for (int i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            double tmp; tmp=re[i]; re[i]=re[j]; re[j]=tmp;
                        tmp=im[i]; im[i]=im[j]; im[j]=tmp;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = -2.0 * M_PI / len;
        double wre = cos(ang), wim = sin(ang);
        for (int i = 0; i < n; i += len) {
            double cur_re = 1.0, cur_im = 0.0;
            for (int j = 0; j < len/2; j++) {
                double u_re = re[i+j],          u_im = im[i+j];
                double v_re = re[i+j+len/2]*cur_re - im[i+j+len/2]*cur_im;
                double v_im = re[i+j+len/2]*cur_im + im[i+j+len/2]*cur_re;
                re[i+j]         = u_re + v_re;
                im[i+j]         = u_im + v_im;
                re[i+j+len/2]   = u_re - v_re;
                im[i+j+len/2]   = u_im - v_im;
                double nre = cur_re*wre - cur_im*wim;
                cur_im = cur_re*wim + cur_im*wre;
                cur_re = nre;
            }
        }
    }
}

/* Next power of 2 >= n */
static int _next_pow2(int n)
{
    int p = 1;
    while (p < n) p <<= 1;
    return p;
}

aw_spectrum_t *aw_spectrum_compute(const float *iq, size_t n_samples,
                                   double sample_rate, double centre_freq)
{
    int n = _next_pow2((int)n_samples / 2);
    if (n < 2) return NULL;

    double *re = calloc(n, sizeof(double));
    double *im = calloc(n, sizeof(double));
    if (!re || !im) { free(re); free(im); return NULL; }

    int take = (n < (int)n_samples/2) ? n : (int)n_samples/2;
    for (int i = 0; i < take; i++) {
        re[i] = iq[2*i];
        im[i] = iq[2*i+1];
    }

    _fft(re, im, n);

    aw_spectrum_t *spec = malloc(sizeof(*spec));
    if (!spec) { free(re); free(im); return NULL; }
    spec->freqs = malloc(n * sizeof(double));
    spec->power = malloc(n * sizeof(float));
    spec->n_bins = n;

    double df = sample_rate / n;
    for (int i = 0; i < n; i++) {
        /* FFT-shift: bin > n/2 wraps to negative freq */
        int k = (i + n/2) % n;
        double mag2 = re[k]*re[k] + im[k]*im[k];
        spec->freqs[i] = centre_freq + (i - n/2) * df;
        spec->power[i] = (mag2 > 0.0)
            ? (float)(10.0 * log10(mag2 / (n * n)) + 30.0) /* dBm, ref 1 mW */
            : -120.0f;
    }

    free(re);
    free(im);
    return spec;
}

void aw_spectrum_free(aw_spectrum_t *s)
{
    if (!s) return;
    free(s->freqs);
    free(s->power);
    free(s);
}

/* ── Log-distance path-loss model ───────────────────────────────────────── */

double aw_path_loss_distance(float rssi_dbm,
                              float path_loss_exp,
                              float ref_dist_m,
                              float rssi_at_ref_dbm)
{
    double diff = (double)(rssi_at_ref_dbm - rssi_dbm);
    return ref_dist_m * pow(10.0, diff / (10.0 * path_loss_exp));
}
