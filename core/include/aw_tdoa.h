#ifndef AW_TDOA_H
#define AW_TDOA_H

#include "aw_types.h"

#define AW_TDOA_MAX_ANTENNAS 16

/* Speed of light, m/s */
#define AW_C 299792458.0

static inline double aw_tdoa_to_range(double tdoa_s) {
    return tdoa_s * AW_C;
}

/*
 * Antenna descriptor for the solver.
 * All positions must be in the same local ENU frame.
 */
typedef struct {
    aw_rel_pos_t position;
    char         id[64];
} aw_tdoa_antenna_t;

typedef struct aw_tdoa_ctx aw_tdoa_ctx_t;

/*
 * Initialise solver with array geometry and the reference antenna ID.
 * The reference antenna is the one whose arrival time is t=0 in all
 * TDOA measurements (other TDOAs can be negative).
 */
aw_tdoa_ctx_t   *aw_tdoa_init(const aw_tdoa_antenna_t *antennas, int n,
                               const char *reference_id);
void             aw_tdoa_destroy(aw_tdoa_ctx_t *ctx);

/*
 * Solve for source position given N TDOA measurements.
 * Uses Gauss-Newton iterative least squares with multi-start seeding
 * (array centroid + 6 axis-displaced seeds).  Requires n_meas >= 3.
 * Returns covariance Cov(s) = (sigma_tau²/dof) · (J'J)⁻¹ at convergence.
 */
aw_tdoa_result_t aw_tdoa_solve(aw_tdoa_ctx_t    *ctx,
                                const aw_tdoa_meas_t *meas,
                                int               n_meas);

#endif /* AW_TDOA_H */
