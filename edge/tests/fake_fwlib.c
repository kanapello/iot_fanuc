/*
 * Minimal fake of the Fanuc FOCAS2 library, used only by the tests of the probe
 * (edge/tools/focas_probe.py) and of the collector driver. Struct layouts mirror
 * fwlib32.h (0i-D / 30i). Only reading functions exist: the system never writes to a CNC.
 *
 * Behaviour is selected by the IP address passed to cnc_allclibhndl3:
 *   10.0.0.1 - healthy machine, running, no alarms
 *   10.0.0.2 - machine in alarm; cnc_sysinfo, cnc_exeprgname, cnc_rdspmeter, cnc_rdsvmeter
 *              unsupported (EW_FUNC), cutting timer EW_NOOPT, G0014 EW_NUMBER, #4120 vacant
 *   anything else - EW_SOCKET
 *
 * fake_calls("cnc_actf") tells how many times a function was called; fake_reset() clears it.
 */
#include <string.h>

#pragma pack(push, 4)
typedef struct { short addinfo; short max_axis; char cnc_type[2]; char mt_type[2]; char series[4]; char version[4]; char axes[2]; } ODBSYS;
typedef struct { short hdck, tmmode, aut, run, motion, mstb, emergency, alarm, edit; } ODBST;
typedef struct { short dummy[2]; short data; short mdata; } ODBPRO;
typedef struct { char name[36]; int o_num; } ODBEXEPRG;
typedef struct { short dummy[2]; int data; } ODBACT;
typedef struct { int data; short dec; short unit; char name; char suff1; char suff2; char reserve; } LOADELM;
typedef struct { LOADELM spload; LOADELM spspeed; } ODBSPLOAD;
typedef struct { LOADELM svload; } ODBSVLOAD;
typedef struct { short datano; short dummy; int mcr_val; short dec_val; } ODBM;
typedef struct { short datano; short type; int ldata; } IODBPSD;
typedef struct { int alm_no; short type; short axis; short dummy; short msg_len; char alm_msg[64]; } ODBALMMSG2;
typedef struct { int minute; int msec; } IODBTIME;
typedef struct { char name; char suff; } ODBAXISNAME;
typedef struct { short type_a; short type_d; unsigned short datano_s; unsigned short datano_e; unsigned char cdata[1]; } IODBPMC;
typedef struct { short datano; short type; unsigned char data[1]; } ODBDGN;
#define MAX_AXIS 32
typedef struct {
    short dummy; short axis; int alarm; int prgnum; int prgmnum; int seqnum; int actf; int acts;
    int absolute[MAX_AXIS]; int machine[MAX_AXIS]; int relative[MAX_AXIS]; int distance[MAX_AXIS];
} ODBDY2;
#pragma pack(pop)

#define EW_OK 0
#define EW_FUNC 1
#define EW_LENGTH 2
#define EW_NUMBER 3
#define EW_ATTRIB 4
#define EW_NOOPT 6
#define EW_HANDLE (-8)
#define EW_SOCKET (-16)

static int mode = 0;

/* ---------------------------------------------------------------- call counting */

#define MAX_NAMES 64
static const char *names[MAX_NAMES];
static int counts[MAX_NAMES];

static void bump(const char *name) {
    for (int i = 0; i < MAX_NAMES; i++) {
        if (names[i] == 0) { names[i] = name; counts[i] = 1; return; }
        if (strcmp(names[i], name) == 0) { counts[i]++; return; }
    }
}

int fake_calls(const char *name) {
    for (int i = 0; i < MAX_NAMES && names[i]; i++)
        if (strcmp(names[i], name) == 0) return counts[i];
    return 0;
}

void fake_reset(void) { memset(names, 0, sizeof(names)); memset(counts, 0, sizeof(counts)); }

/* ---------------------------------------------------------------- connection */

short cnc_startupprocess(int level, const char *filename) { (void)level; (void)filename; return EW_OK; }
short cnc_exitprocess(void) { return EW_OK; }

short cnc_allclibhndl3(const char *ip, unsigned short port, int timeout, unsigned short *h) {
    (void)timeout;
    bump("cnc_allclibhndl3");
    if (port != 8193) return EW_SOCKET;
    if (strcmp(ip, "10.0.0.1") == 0) mode = 1;
    else if (strcmp(ip, "10.0.0.2") == 0) mode = 2;
    else return EW_SOCKET;
    *h = 7;
    return EW_OK;
}

short cnc_freelibhndl(unsigned short h) { (void)h; mode = 0; return EW_OK; }

#define CHECK_HANDLE(name) bump(name); if (h != 7 || mode == 0) return EW_HANDLE

/* ---------------------------------------------------------------- the minimum */

short cnc_sysinfo(unsigned short h, ODBSYS *s) {
    CHECK_HANDLE("cnc_sysinfo");
    if (mode == 2) return EW_FUNC;
    memset(s, 0, sizeof(*s));
    s->addinfo = 0x0002;  /* i series */
    s->max_axis = 32;
    memcpy(s->cnc_type, " 0", 2); memcpy(s->mt_type, " M", 2);
    memcpy(s->series, "D4F1", 4); memcpy(s->version, "16.0", 4); memcpy(s->axes, "03", 2);
    return EW_OK;
}

short cnc_statinfo(unsigned short h, ODBST *s) {
    CHECK_HANDLE("cnc_statinfo");
    memset(s, 0, sizeof(*s));
    s->tmmode = 1; s->aut = 1;
    if (mode == 1) { s->run = 3; s->motion = 1; }
    else { s->run = 1; s->alarm = 1; }
    return EW_OK;
}

short cnc_rdprgnum(unsigned short h, ODBPRO *p) {
    CHECK_HANDLE("cnc_rdprgnum");
    p->data = 1234; p->mdata = 1000;
    return EW_OK;
}

short cnc_exeprgname(unsigned short h, ODBEXEPRG *p) {
    CHECK_HANDLE("cnc_exeprgname");
    if (mode == 2) return EW_FUNC;
    memset(p, 0, sizeof(*p));
    strcpy(p->name, "O1234");
    p->o_num = 1234;
    return EW_OK;
}

short cnc_rdmacro(unsigned short h, short number, short length, ODBM *m) {
    CHECK_HANDLE("cnc_rdmacro");
    (void)length;
    m->datano = number;
    if (number == 3901) { m->mcr_val = 15873000; m->dec_val = 3; return EW_OK; }
    if (number == 4120) {
        if (mode == 2) { m->mcr_val = 0; m->dec_val = -1; return EW_OK; }  /* <vacant> */
        m->mcr_val = 12000; m->dec_val = 3;
        return EW_OK;
    }
    return EW_NUMBER;
}

short cnc_rdparam(unsigned short h, short number, short axis, short length, IODBPSD *p) {
    CHECK_HANDLE("cnc_rdparam");
    (void)axis; (void)length;
    if (number != 6711) return EW_NUMBER;
    p->datano = number; p->ldata = 15873;
    return EW_OK;
}

short cnc_rdalmmsg2(unsigned short h, short type, short *num, ODBALMMSG2 *a) {
    CHECK_HANDLE("cnc_rdalmmsg2");
    (void)type;
    if (mode != 2) { *num = 0; return EW_OK; }
    memset(a, 0, sizeof(*a));
    a[0].alm_no = 401; a[0].type = 6; a[0].axis = 1;
    strcpy(a[0].alm_msg, "V-READY OFF");
    a[0].msg_len = (short)strlen(a[0].alm_msg);
    *num = 1;
    return EW_OK;
}

/* ---------------------------------------------------------------- extended (recon, opt-in signals) */

short cnc_actf(unsigned short h, ODBACT *a) { CHECK_HANDLE("cnc_actf"); a->data = (mode == 1) ? 1250 : 0; return EW_OK; }
short cnc_acts(unsigned short h, ODBACT *a) { CHECK_HANDLE("cnc_acts"); a->data = (mode == 1) ? 8000 : 0; return EW_OK; }

short cnc_rdspmeter(unsigned short h, short type, short *num, ODBSPLOAD *l) {
    CHECK_HANDLE("cnc_rdspmeter");
    (void)type;
    if (mode == 2) return EW_FUNC;
    if (*num < 1) return EW_NUMBER;
    memset(l, 0, sizeof(*l));
    l[0].spload.data = 375; l[0].spload.dec = 1; l[0].spload.name = 'S'; l[0].spload.suff1 = '1';
    *num = 1;
    return EW_OK;
}

short cnc_rdaxisname(unsigned short h, short *num, ODBAXISNAME *a) {
    CHECK_HANDLE("cnc_rdaxisname");
    if (*num < 3) return EW_LENGTH;
    a[0].name = 'X'; a[0].suff = 0; a[1].name = 'Y'; a[1].suff = 0; a[2].name = 'Z'; a[2].suff = 0;
    *num = 3;
    return EW_OK;
}

short cnc_rdsvmeter(unsigned short h, short *num, ODBSVLOAD *l) {
    CHECK_HANDLE("cnc_rdsvmeter");
    if (mode == 2) return EW_FUNC;
    if (*num < 3) return EW_LENGTH;
    int loads[3] = {125, 80, 250};
    for (int i = 0; i < 3; i++) {
        memset(&l[i], 0, sizeof(l[i]));
        l[i].svload.data = loads[i]; l[i].svload.dec = 1; l[i].svload.name = "XYZ"[i];
    }
    *num = 3;
    return EW_OK;
}

short cnc_rdtimer(unsigned short h, short type, IODBTIME *t) {
    CHECK_HANDLE("cnc_rdtimer");
    if (type < 0 || type > 4) return EW_NUMBER;
    if (mode == 2 && type == 2) return EW_NOOPT;
    int minutes[5] = {123456, 54321, 9876, 1, 0};
    int msec[5] = {0, 30000, 45500, 12345, 0};
    t->minute = minutes[type]; t->msec = msec[type];
    return EW_OK;
}

short cnc_rddynamic2(unsigned short h, short axis, short length, ODBDY2 *d) {
    CHECK_HANDLE("cnc_rddynamic2");
    if (axis != -1 || length != (short)sizeof(ODBDY2)) return EW_LENGTH;
    memset(d, 0, sizeof(*d));
    d->axis = -1; d->prgnum = 1234; d->prgmnum = 1000; d->seqnum = 120;
    d->actf = (mode == 1) ? 1250 : 0; d->acts = (mode == 1) ? 8000 : 0; d->alarm = (mode == 2) ? 1 : 0;
    for (int i = 0; i < 3; i++) {
        d->absolute[i] = 10000 * (i + 1); d->machine[i] = 10000 * (i + 1) + 5;
        d->relative[i] = 100 * (i + 1); d->distance[i] = 0;
    }
    return EW_OK;
}

/* G0012 = 0x9B: feed override 100 % in negative logic; G0030 = 90 %; G0014 = ROV1: 50 %;
   X/Y bytes: a pattern of the address. */
short pmc_rdpmcrng(unsigned short h, short adr_type, short data_type, unsigned short start,
                   unsigned short end, unsigned short length, IODBPMC *buf) {
    CHECK_HANDLE("pmc_rdpmcrng");
    if (data_type != 0 || end < start || length != 8 + (end - start + 1)) return EW_LENGTH;
    buf->type_a = adr_type; buf->type_d = data_type; buf->datano_s = start; buf->datano_e = end;
    for (unsigned short a = start; a <= end; a++) {
        unsigned char v;
        if (adr_type == 0 && a == 12) v = 0x9B;
        else if (adr_type == 0 && a == 30) v = 90;
        else if (adr_type == 0 && a == 14) { if (mode == 2) return EW_NUMBER; v = 0x01; }
        else if (adr_type == 2 || adr_type == 3) v = (unsigned char)(a * 3 + adr_type);
        else v = 0;
        buf->cdata[a - start] = v;
    }
    return EW_OK;
}

/* 1000 - one byte; 300 - an axis type, two-word per axis; others - EW_NUMBER */
short cnc_diagnoss(unsigned short h, short number, short axis, short length, ODBDGN *d) {
    CHECK_HANDLE("cnc_diagnoss");
    d->datano = number; d->type = axis;
    if (number == 1000) {
        if (axis != 0) return EW_ATTRIB;
        if (length != 4 + 1) return EW_LENGTH;
        d->data[0] = 0x5A;
        return EW_OK;
    }
    if (number == 300) {
        if (axis == 0) return EW_ATTRIB;
        if (length != 4 + 4 * MAX_AXIS) return EW_LENGTH;
        int *values = (int *)d->data;
        for (int i = 0; i < MAX_AXIS; i++) values[i] = (i < 3) ? -(i + 1) * 7 : 0;
        return EW_OK;
    }
    return EW_NUMBER;
}
