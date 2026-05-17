#include "aw_tdoa.h"

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

/*
 * Gauss-Newton iterative TDOA solver.
 *
 * Directly minimises Σ (τ_i^pred - τ_i^meas)² over source position s.
 * No extra unknown (d0), works correctly with ≥ 3 non-reference sensors.
 *
 * τ_i^pred = (||s - pi|| - ||s - p0||) / c
 *
 * Jacobian row i:
 *   J[i] = (1/c) * [(s-pi)/||s-pi|| - (s-p0)/||s-p0||]
 *
 * Update: Δs = -(J'J)^{-1} J' r
 */

#define MAX_ITER   80
#define TOL        1e-7

struct aw_tdoa_ctx {
    aw_tdoa_antenna_t antennas[AW_TDOA_MAX_ANTENNAS];
    int               n;
    int               ref_idx;
};

/* ── 3×3 linear system solver (Cramer's rule) ───────────────────────────── */

static int solve3(const double A[3][3], const double b[3], double x[3])
{
    double det = A[0][0]*(A[1][1]*A[2][2]-A[1][2]*A[2][1])
               - A[0][1]*(A[1][0]*A[2][2]-A[1][2]*A[2][0])
               + A[0][2]*(A[1][0]*A[2][1]-A[1][1]*A[2][0]);
    if (fabs(det) < 1e-30) return -1;
    double inv = 1.0 / det;
    x[0] = inv*( b[0]*(A[1][1]*A[2][2]-A[1][2]*A[2][1])
               - A[0][1]*(b[1]*A[2][2]-A[1][2]*b[2])
               + A[0][2]*(b[1]*A[2][1]-A[1][1]*b[2]));
    x[1] = inv*(A[0][0]*(b[1]*A[2][2]-A[1][2]*b[2])
               - b[0]*(A[1][0]*A[2][2]-A[1][2]*A[2][0])
               + A[0][2]*(A[1][0]*b[2]-b[1]*A[2][0]));
    x[2] = inv*(A[0][0]*(A[1][1]*b[2]-b[1]*A[2][1])
               - A[0][1]*(A[1][0]*b[2]-b[1]*A[2][0])
               + b[0]*(A[1][0]*A[2][1]-A[1][1]*A[2][0]));
    return 0;
}

/* ── Single Gauss-Newton run from one starting point ────────────────────── */

static double _gn_run(const double p0[3],
                       const double (*P)[3], const double *T, int M,
                       double s[3])
{
    for (int iter = 0; iter < MAX_ITER; iter++) {
        /* ranges */
        double dx0 = s[0]-p0[0], dy0 = s[1]-p0[1], dz0 = s[2]-p0[2];
        double d0 = sqrt(dx0*dx0 + dy0*dy0 + dz0*dz0);
        if (d0 < 1e-9) d0 = 1e-9;

        /* J'J and J'r accumulation */
        double JtJ[3][3] = {0};
        double Jtr[3]    = {0};
        double rms2 = 0.0;

        for (int i = 0; i < M; i++) {
            double dxi = s[0]-P[i][0], dyi = s[1]-P[i][1], dzi = s[2]-P[i][2];
            double di  = sqrt(dxi*dxi + dyi*dyi + dzi*dzi);
            if (di < 1e-9) di = 1e-9;

            double tau_pred = (di - d0) / AW_C;
            double ri = tau_pred - T[i];
            rms2 += ri * ri;

            /* J row: (1/c)*[(s-pi)/di - (s-p0)/d0] */
            double J[3] = {
                (1.0/AW_C) * (dxi/di - dx0/d0),
                (1.0/AW_C) * (dyi/di - dy0/d0),
                (1.0/AW_C) * (dzi/di - dz0/d0),
            };
            for (int r = 0; r < 3; r++) {
                for (int c = 0; c < 3; c++)
                    JtJ[r][c] += J[r]*J[c];
                Jtr[r] += J[r] * ri;
            }
        }

        double neg_Jtr[3] = {-Jtr[0], -Jtr[1], -Jtr[2]};
        double delta[3];
        if (solve3(JtJ, neg_Jtr, delta) != 0) break;

        s[0] += delta[0];
        s[1] += delta[1];
        s[2] += delta[2];

        double norm_d = sqrt(delta[0]*delta[0]+delta[1]*delta[1]+delta[2]*delta[2]);
        if (norm_d < TOL) break;
    }

    /* compute final RMS (in metres) */
    double dx0 = s[0]-p0[0], dy0 = s[1]-p0[1], dz0 = s[2]-p0[2];
    double d0 = sqrt(dx0*dx0 + dy0*dy0 + dz0*dz0);
    if (d0 < 1e-9) d0 = 1e-9;
    double rms2 = 0.0;
    for (int i = 0; i < M; i++) {
        double dxi = s[0]-P[i][0], dyi = s[1]-P[i][1], dzi = s[2]-P[i][2];
        double di = sqrt(dxi*dxi + dyi*dyi + dzi*dzi);
        if (di < 1e-9) di = 1e-9;
        double r = (di - d0) / AW_C - T[i];
        rms2 += r * r;
    }
    return sqrt(rms2 / M) * AW_C;
}

/* ── Public API ─────────────────────────────────────────────────────────── */

aw_tdoa_ctx_t *aw_tdoa_init(const aw_tdoa_antenna_t *antennas, int n,
                             const char *reference_id)
{
    if (n < 2 || n > AW_TDOA_MAX_ANTENNAS) return NULL;
    aw_tdoa_ctx_t *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) return NULL;
    memcpy(ctx->antennas, antennas, n * sizeof(*antennas));
    ctx->n       = n;
    ctx->ref_idx = 0;
    for (int i = 0; i < n; i++) {
        if (strcmp(antennas[i].id, reference_id) == 0) {
            ctx->ref_idx = i;
            break;
        }
    }
    return ctx;
}

void aw_tdoa_destroy(aw_tdoa_ctx_t *ctx) { free(ctx); }

aw_tdoa_result_t aw_tdoa_solve(aw_tdoa_ctx_t        *ctx,
                                const aw_tdoa_meas_t *meas,
                                int                   n_meas)
{
    aw_tdoa_result_t res = {0};

    const double p0[3] = {
        ctx->antennas[ctx->ref_idx].position.x,
        ctx->antennas[ctx->ref_idx].position.y,
        ctx->antennas[ctx->ref_idx].position.z,
    };

    /* Collect matched sensors */
    double P[AW_TDOA_MAX_ANTENNAS][3];
    double T[AW_TDOA_MAX_ANTENNAS];
    int    M = 0;

    for (int m = 0; m < n_meas && M < AW_TDOA_MAX_ANTENNAS; m++) {
        if (strcmp(meas[m].antenna_id,
                   ctx->antennas[ctx->ref_idx].id) == 0) continue;
        for (int a = 0; a < ctx->n; a++) {
            if (a == ctx->ref_idx) continue;
            if (strcmp(meas[m].antenna_id, ctx->antennas[a].id) != 0) continue;
            P[M][0] = ctx->antennas[a].position.x;
            P[M][1] = ctx->antennas[a].position.y;
            P[M][2] = ctx->antennas[a].position.z;
            T[M]    = meas[m].tdoa;
            M++;
            break;
        }
    }
    if (M < 3) return res;

    /* Multi-start: centroid + 6 axis-displaced seeds */
    double cx = p0[0], cy = p0[1], cz = p0[2];
    for (int i = 0; i < M; i++) { cx += P[i][0]; cy += P[i][1]; cz += P[i][2]; }
    cx /= (M+1); cy /= (M+1); cz /= (M+1);

    double spread = 0.0;
    for (int i = 0; i < M; i++) {
        double d = sqrt((P[i][0]-p0[0])*(P[i][0]-p0[0])
                      + (P[i][1]-p0[1])*(P[i][1]-p0[1])
                      + (P[i][2]-p0[2])*(P[i][2]-p0[2]));
        if (d > spread) spread = d;
    }
    if (spread < 1.0) spread = 10.0;
    spread *= 2.0;

    static const double dirs[7][3] = {
        {0,0,0},{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}
    };
    double best_s[3] = {0};
    double best_rms  = 1e18;

    for (int d = 0; d < 7; d++) {
        double s[3] = {
            cx + dirs[d][0]*spread,
            cy + dirs[d][1]*spread,
            cz + dirs[d][2]*spread,
        };
        double rms = _gn_run(p0, (const double (*)[3])P, T, M, s);
        if (rms < best_rms) {
            best_rms  = rms;
            best_s[0] = s[0]; best_s[1] = s[1]; best_s[2] = s[2];
        }
    }

    res.position.x      = best_s[0];
    res.position.y      = best_s[1];
    res.position.z      = best_s[2];
    res.position.source = AW_REL_SRC_TDOA;
    res.residual        = (float)best_rms;
    res.valid           = 1;
    res.n_meas          = M;

    /*
     * Covariance from Fisher information at convergence:
     *   Cov(s) = (sigma_tau² / dof) · (J'J)⁻¹
     *
     * sigma_tau = RMS timing residual = best_rms / c
     * dof       = max(1, M - 3)
     *
     * Invert J'J using Cramer's rule (solve3 solves Ax=b; three right-hand
     * sides = identity columns gives the columns of the inverse).
     */
    {
        double dx0f = best_s[0]-p0[0], dy0f = best_s[1]-p0[1], dz0f = best_s[2]-p0[2];
        double d0f  = sqrt(dx0f*dx0f + dy0f*dy0f + dz0f*dz0f);
        if (d0f < 1e-9) d0f = 1e-9;

        double JtJ[3][3] = {0};
        for (int i = 0; i < M; i++) {
            double dxi = best_s[0]-P[i][0], dyi = best_s[1]-P[i][1], dzi = best_s[2]-P[i][2];
            double di  = sqrt(dxi*dxi + dyi*dyi + dzi*dzi);
            if (di < 1e-9) di = 1e-9;
            double J[3] = {
                (1.0/AW_C) * (dxi/di - dx0f/d0f),
                (1.0/AW_C) * (dyi/di - dy0f/d0f),
                (1.0/AW_C) * (dzi/di - dz0f/d0f),
            };
            for (int r = 0; r < 3; r++)
                for (int c = 0; c < 3; c++)
                    JtJ[r][c] += J[r]*J[c];
        }

        double sigma2 = (best_rms / AW_C) * (best_rms / AW_C);
        double scale  = sigma2 / (M > 3 ? M - 3 : 1);

        /* Invert J'J column by column: JtJ · col_k = e_k */
        double inv[3][3];
        int ok = 1;
        for (int k = 0; k < 3; k++) {
            double ek[3] = {0}; ek[k] = 1.0;
            double col[3];
            if (solve3(JtJ, ek, col) != 0) { ok = 0; break; }
            inv[0][k] = col[0]; inv[1][k] = col[1]; inv[2][k] = col[2];
        }

        memset(res.position.cov, 0, sizeof(res.position.cov));
        if (ok) {
            for (int r = 0; r < 3; r++)
                for (int c = 0; c < 3; c++)
                    res.position.cov[r*3+c] = scale * inv[r][c];
        } else {
            /* Singular J'J (degenerate geometry) — fall back to scalar diagonal */
            double var = best_rms * best_rms;
            res.position.cov[0] = var;
            res.position.cov[4] = var;
            res.position.cov[8] = var;
        }
    }

    return res;
}
