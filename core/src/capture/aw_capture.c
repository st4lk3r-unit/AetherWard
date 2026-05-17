#include "aw_capture.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#include <pthread.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/wait.h>

#ifdef AW_HAVE_PCAP
#include <pcap/pcap.h>

/* Radiotap header: we only extract the fields we need. */
#define RADIOTAP_F_TSFT       (1 << 0)
#define RADIOTAP_F_FLAGS      (1 << 1)
#define RADIOTAP_F_RATE       (1 << 2)
#define RADIOTAP_F_CHANNEL    (1 << 3)
#define RADIOTAP_F_DBM_SIGNAL (1 << 5)

struct radiotap_hdr {
    uint8_t  it_version;
    uint8_t  it_pad;
    uint16_t it_len;
    uint32_t it_present;
} __attribute__((packed));

struct aw_capture_ctx {
    pcap_t          *pcap;
    char             iface[64];
    char             antenna_id[64];
    aw_frame_cb_t    cb;
    void            *user;
    pthread_t        thread;
    volatile int     running;
};

/* ── Internal helpers ───────────────────────────────────────────────────── */

static double _ts_to_epoch(const struct pcap_pkthdr *hdr)
{
    return (double)hdr->ts.tv_sec + (double)hdr->ts.tv_usec * 1e-6;
}

/*
 * Return a pointer to the first radiotap field byte, skipping the fixed
 * header and any chained it_present extension words (bit 31 set = another
 * present word follows, per the radiotap spec).
 * Also returns the first it_present bitmap via *present_out.
 */
static const uint8_t *_rt_fields(const uint8_t *pkt, uint16_t rt_len,
                                  uint32_t *present_out)
{
    const struct radiotap_hdr *rth = (const struct radiotap_hdr *)pkt;
    uint32_t present = rth->it_present;
    *present_out = present;

    /* Skip the fixed header (8 bytes) then any extension present words.
     * Each extension present word is 4 bytes; its bit 31 flags yet another. */
    const uint8_t *p = pkt + sizeof(*rth);
    while ((present & 0x80000000u) && p + 4 <= pkt + rt_len) {
        memcpy(&present, p, 4);  /* read extension word, check its bit 31 */
        p += 4;                  /* advance past it */
    }
    return p;
}

static float _parse_rssi(const uint8_t *pkt, uint16_t rt_len)
{
    uint32_t present;
    const uint8_t *p = _rt_fields(pkt, rt_len, &present);

    if (present & RADIOTAP_F_TSFT)  p += 8;
    if (present & RADIOTAP_F_FLAGS) p += 1;
    if (present & RADIOTAP_F_RATE)  p += 1;
    if (present & RADIOTAP_F_CHANNEL) {
        uintptr_t off = (uintptr_t)(p - pkt);
        if (off & 1) p++;
        p += 4;
    }
    if (present & RADIOTAP_F_DBM_SIGNAL) {
        if (p + 1 <= pkt + rt_len)
            return (float)(int8_t)(*p);
    }
    return 0.0f;
}

static double _frame_frequency(const uint8_t *pkt, uint16_t rt_len)
{
    uint32_t present;
    const uint8_t *p = _rt_fields(pkt, rt_len, &present);

    if (present & RADIOTAP_F_TSFT)  p += 8;
    if (present & RADIOTAP_F_FLAGS) p += 1;
    if (present & RADIOTAP_F_RATE)  p += 1;
    if (present & RADIOTAP_F_CHANNEL) {
        uintptr_t off = (uintptr_t)(p - pkt);
        if (off & 1) p++;
        if (p + 2 <= pkt + rt_len) {
            uint16_t mhz;
            memcpy(&mhz, p, 2);
            return (double)mhz * 1e6;
        }
    }
    return 2412e6;
}

static void _pcap_handler(u_char *arg,
                           const struct pcap_pkthdr *hdr,
                           const u_char *pkt)
{
    struct aw_capture_ctx *ctx = (struct aw_capture_ctx *)arg;
    if (!ctx->running || hdr->caplen < sizeof(struct radiotap_hdr))
        return;

    const struct radiotap_hdr *rth = (const struct radiotap_hdr *)pkt;
    uint16_t rt_len = rth->it_len;

    aw_frame_t frame = {0};
    frame.timestamp  = _ts_to_epoch(hdr);
    frame.rssi       = _parse_rssi(pkt, rt_len);
    frame.frequency  = _frame_frequency(pkt, rt_len);
    frame.bandwidth  = 20e6;
    frame.data_len   = hdr->caplen - rt_len;
    frame.data       = (uint8_t *)(pkt + rt_len);
    memcpy(frame.antenna_id, ctx->antenna_id, sizeof(frame.antenna_id));

    ctx->cb(&frame, ctx->user);
}

static void *_capture_thread(void *arg)
{
    struct aw_capture_ctx *ctx = arg;
    pcap_loop(ctx->pcap, -1, _pcap_handler, (u_char *)ctx);
    return NULL;
}

/* ── Safe iw runner — no shell, no injection ────────────────────────────── */

static int _run_iw(const char *const argv[])
{
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        /* Redirect stdout/stderr to /dev/null */
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        execvp("iw", (char *const *)argv);
        _exit(127);
    }
    int status;
    if (waitpid(pid, &status, 0) < 0) return -1;
    return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : -1;
}

/* ── Public API ─────────────────────────────────────────────────────────── */

aw_capture_ctx_t *aw_capture_open(const char *iface, aw_frame_cb_t cb, void *user)
{
    char errbuf[PCAP_ERRBUF_SIZE];
    pcap_t *pcap = pcap_open_live(iface, 65535, 1, 100, errbuf);
    if (!pcap) {
        fprintf(stderr, "aw_capture: pcap_open_live(%s): %s\n", iface, errbuf);
        return NULL;
    }

    if (pcap_set_datalink(pcap, DLT_IEEE802_11_RADIO) != 0) {
        fprintf(stderr, "aw_capture: cannot set radiotap datalink\n");
        pcap_close(pcap);
        return NULL;
    }

    struct aw_capture_ctx *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) { pcap_close(pcap); return NULL; }

    ctx->pcap = pcap;
    ctx->cb   = cb;
    ctx->user = user;
    strncpy(ctx->iface,      iface, sizeof(ctx->iface) - 1);
    strncpy(ctx->antenna_id, iface, sizeof(ctx->antenna_id) - 1);

    return ctx;
}

int aw_capture_set_channel(aw_capture_ctx_t *ctx, int channel)
{
    char ch_str[16];
    snprintf(ch_str, sizeof(ch_str), "%d", channel);
    const char *argv[] = {"iw", "dev", ctx->iface, "set", "channel", ch_str, NULL};
    return _run_iw(argv);
}

int aw_capture_set_frequency(aw_capture_ctx_t *ctx, double hz)
{
    char mhz_str[16];
    snprintf(mhz_str, sizeof(mhz_str), "%d", (int)(hz / 1e6));
    const char *argv[] = {"iw", "dev", ctx->iface, "set", "freq", mhz_str, NULL};
    return _run_iw(argv);
}

int aw_capture_start(aw_capture_ctx_t *ctx)
{
    ctx->running = 1;
    return pthread_create(&ctx->thread, NULL, _capture_thread, ctx);
}

void aw_capture_stop(aw_capture_ctx_t *ctx)
{
    ctx->running = 0;
    pcap_breakloop(ctx->pcap);
    pthread_join(ctx->thread, NULL);
}

void aw_capture_close(aw_capture_ctx_t *ctx)
{
    if (!ctx) return;
    pcap_close(ctx->pcap);
    free(ctx);
}

int aw_capture_assign_channels(aw_capture_ctx_t **ctxs, int n_ctx,
                                aw_chan_assign_t  *assigns)
{
    for (int i = 0; i < n_ctx; i++) {
        strncpy(ctxs[i]->antenna_id, assigns[i].antenna_id,
                sizeof(ctxs[i]->antenna_id) - 1);
        if (assigns[i].n_channels > 0)
            aw_capture_set_channel(ctxs[i], assigns[i].channels[0]);
    }
    return 0;
}

#else /* AW_HAVE_PCAP not defined — stub implementations */

aw_capture_ctx_t *aw_capture_open(const char *iface, aw_frame_cb_t cb, void *user)
{
    (void)iface; (void)cb; (void)user;
    fprintf(stderr, "aw_capture: built without libpcap — use Python scapy backend\n");
    return NULL;
}

int  aw_capture_set_channel(aw_capture_ctx_t *ctx, int ch)    { (void)ctx; (void)ch;  return -1; }
int  aw_capture_set_frequency(aw_capture_ctx_t *ctx, double f) { (void)ctx; (void)f;   return -1; }
int  aw_capture_start(aw_capture_ctx_t *ctx)                   { (void)ctx;            return -1; }
void aw_capture_stop(aw_capture_ctx_t *ctx)                    { (void)ctx; }
void aw_capture_close(aw_capture_ctx_t *ctx)                   { (void)ctx; }
int  aw_capture_assign_channels(aw_capture_ctx_t **ctxs, int n,
                                 aw_chan_assign_t *assigns)
{
    (void)ctxs; (void)n; (void)assigns; return -1;
}

#endif /* AW_HAVE_PCAP */
