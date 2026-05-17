#ifndef AW_TYPES_H
#define AW_TYPES_H

#include <stdint.h>
#include <stddef.h>

/* ── GNSS fix quality ───────────────────────────────────────────────────── */
#define AW_FIX_NONE       0
#define AW_FIX_2D         1
#define AW_FIX_3D         2
#define AW_FIX_DGPS       3
#define AW_FIX_RTK_FLOAT  4
#define AW_FIX_RTK_FIXED  5

/* ── Relative-position origin ───────────────────────────────────────────── */
#define AW_REL_SRC_IMU      0
#define AW_REL_SRC_ENCODER  1
#define AW_REL_SRC_MANUAL   2
#define AW_REL_SRC_TDOA     3

/* ── Orientation origin ─────────────────────────────────────────────────── */
#define AW_ORI_SRC_IMU      0
#define AW_ORI_SRC_COMPASS  1
#define AW_ORI_SRC_MANUAL   2

/*
 * AbsolutePosition — geodetic anchor, WGS84.
 *
 * Represents a GNSS fix. Used to anchor the local ENU frame to the real
 * world and to tag observations with real-world coordinates.
 * Never used directly inside the TDOA solver or array geometry math.
 */
typedef struct {
    double  lat;          /* decimal degrees, [-90, 90]   */
    double  lon;          /* decimal degrees, [-180, 180] */
    double  alt;          /* metres above WGS84 ellipsoid */
    float   accuracy_h;   /* horizontal CEP, metres        */
    float   accuracy_v;   /* vertical accuracy, metres     */
    double  timestamp;    /* Unix epoch, sub-second        */
    uint8_t fix_type;     /* AW_FIX_*                      */
    uint8_t num_sats;     /* satellites used in fix        */
} aw_abs_pos_t;

/*
 * RelativePosition — local East-North-Up (ENU) Cartesian frame, metres.
 *
 * This is where all geometry math lives: inter-antenna offsets, TDOA
 * solver inputs/outputs, IMU dead-reckoning.  When an anchor is present
 * the point can be projected back to absolute coordinates, but that
 * transform is always an explicit, separate step.
 */
typedef struct {
    double  x, y, z;     /* metres, ENU                                 */
    double  cov[9];       /* 3×3 covariance matrix, row-major, metres²   */
    double  timestamp;    /* Unix epoch                                  */
    uint8_t source;       /* AW_REL_SRC_*                                */
    int     has_anchor;   /* 1 → anchor field below is valid             */
    aw_abs_pos_t anchor;  /* absolute origin of this local frame         */
} aw_rel_pos_t;

/*
 * Orientation — unit quaternion (w, x, y, z).
 * Represents the rotation from the body frame to the local ENU frame.
 */
typedef struct {
    double  w, x, y, z;  /* unit quaternion                  */
    float   accuracy;     /* estimated error, degrees         */
    double  timestamp;
    uint8_t source;       /* AW_ORI_SRC_*                     */
} aw_orientation_t;

/*
 * Frame — a single captured RF event.
 * Intentionally frequency-agnostic: works for WiFi, SDR captures, etc.
 */
typedef struct {
    double   timestamp;       /* Unix epoch, nanosecond precision when available */
    double   frequency;       /* centre frequency, Hz                            */
    double   bandwidth;       /* capture bandwidth, Hz                           */
    float    rssi;            /* received power at antenna port, dBm             */
    uint8_t *data;            /* raw frame bytes (caller owns allocation)        */
    size_t   data_len;
    char     antenna_id[64];
} aw_frame_t;

/*
 * TDOAMeasurement — time-difference of arrival for one antenna,
 * relative to the designated reference antenna.
 */
typedef struct {
    double timestamp;         /* arrival time at this antenna, Unix epoch */
    double tdoa;              /* seconds; negative means arrived earlier  */
    float  rssi;              /* dBm at this antenna                      */
    char   antenna_id[64];
} aw_tdoa_meas_t;

/*
 * TDOAResult — output of the position solver.
 * Position is always expressed in the local ENU frame first.
 * Projection to absolute coordinates is a separate step in Python.
 */
typedef struct {
    aw_rel_pos_t position;   /* solved source position, local ENU frame */
    float        residual;   /* RMS fit residual, metres                */
    int          valid;      /* 1 = solution converged                  */
    int          n_meas;     /* measurements used                       */
} aw_tdoa_result_t;

#endif /* AW_TYPES_H */
